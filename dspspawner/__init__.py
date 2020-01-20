from dockerspawner import DockerSpawner, SwarmSpawner
from repo2docker.app import Repo2Docker
from wrapspawner import ProfilesSpawner, WrapSpawner
from concurrent.futures import ThreadPoolExecutor
from tornado.ioloop import IOLoop
import asyncio
from escapism import escape
import docker.errors
from docker.types import Mount
import os
import json
import re
import urllib.request
from tornado import gen, concurrent
from jupyterhub.spawner import LocalProcessSpawner, Spawner
from traitlets import (
    Instance, Type, Tuple, List, Dict, Integer, Unicode, Float, Any
)
from traitlets import directional_link

async def subprocess_output(cmd, **kwargs):
    """
    Run cmd until completion & return stdout, stderr

    Convenience method to start and run a process
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs)

    stdout, stderr = await proc.communicate() 

    return stdout.decode(), stderr.decode()

async def resolve_ref(repo_url, ref):
    """
    Return resolved commit hash for branch / tag.

    Return ref unmodified if branch / tag isn't found
    """
    stdout, stderr = await subprocess_output(
        ['git', 'ls-remote', repo_url]
    )
    # ls-remote output looks like this:
    # <hash>\t<ref>\n
    # <hash>\t<ref>\n
    # Since our ref can be a tag (so refs/tags/<ref>) or branch
    # (so refs/head/<ref>), we get all refs and check if either
    # exists
    all_refs = [l.split('\t') for l in stdout.strip().split('\n')]
    for hash, ref in all_refs:
        if ref in (f'refs/heads/{ref}', f'refs/heads/{ref}'):
            return hash

    if stdout:
        return stdout.split()[0]
    return ref

class DSPSwarmSpawner(SwarmSpawner):
    @property
    def mounts(self):
        if len(self.volume_binds):
            driver = self.mount_driver_config
            return [
                Mount(
                    target=vol["bind"],
                    source=host_loc,
                    type="bind",
                    read_only=vol["mode"] == "ro",
                    driver_config=None,
                )
                for host_loc, vol in self.volume_binds.items()
            ]

        else:
            return []

class DSPProfilesSpawner(ProfilesSpawner):
    network_name = Unicode(
        None,
        allow_none=True,
        config=True,
        help="""
        Network name of jupyterhub.

        Should not be None
        """
        )
    
    profiles = List(
        trait = Tuple( Unicode(), Unicode(), Type(Spawner), Dict() ),
        default_value = [ ( 'Normal Environment', 'singleuser', 'dspspawner.DSPSwarmSpawner',
                            dict(image = 'cdasdsp/datasci-rstudio-notebook:latest') ) ],
        minlen = 1,
        config = True,
        help = """List of profiles to offer for selection.  See original version of ProfilesSpawner"""
        )

    child_profile = Unicode()

    form_template = Unicode(
        """<label for="profile">Select your environment:</label>

        <select class="form-control" name="profile" required autofocus>

        {input_template}

        </select>

        <br/>

        <label for="repolink">Repository URL (only necessary for Repo2Docker environment):</label>

        <input class="form-control" name="repolink" type="url" value="https://github.com/...">

        <label for="warning">Spawning a Repo2Docker link could take a long time depending on your configuration.</label>
        """,
        config = True,
        help = """Template to use to construct options_form text. {input_template} is replaced with
            the result of formatting input_template against each item in the profiles list."""
        )

    def options_from_form(self, formdata):
        # Default to first profile if somehow none is provided
        return dict(profile=formdata.get('profile', [self.profiles[0][1]])[0],
                    repolink=formdata.get('repolink')[0])


    def select_profile(self, profile, repolink):
        # Select matching profile, or do nothing (leaving previous or default config in place)
        for p in self.profiles:
            if p[1] == profile:
                self.child_class = p[2]
                self.child_config = p[3]
                if p[1] == 'repo2docker':
                    self.child_config = dict(repo = repolink)
                break

    def construct_child(self):
        self.child_profile = self.user_options.get('profile', "")
        self.repolink = self.user_options.get('repolink', "")
        self.select_profile(self.child_profile, self.repolink)
        WrapSpawner.construct_child(self)

    def load_child_class(self, state):
        try:
            self.child_profile = state['profile']
            self.repolink = state['repolink']
        except KeyError:
            self.child_profile = ''
            self.repolink = ''
        self.select_profile(self.child_profile, self.repolink)

    def get_state(self):
        state = super().get_state()
        state['profile'] = self.child_profile
        state['repolink'] = self.repolink
        return state

    def clear_state(self):
        super().clear_state()
        self.child_profile = ''
        self.repolink = ''



class Repo2DockerSpawner(DSPSwarmSpawner):
    # ThreadPool for talking to r2d
    _r2d_executor = None

    def run_in_executor(self, func, *args):
        # FIXME: This shouldn't be used for anything other than r2d.build
        cls = self.__class__
        if cls._r2d_executor is None:
            # FIXME: Figure out what is correct number here
            cls._r2d_executor = ThreadPoolExecutor(1)
        return IOLoop.current().run_in_executor(cls._r2d_executor, func, *args)


    #start_timeout = 10 * 60

    # We don't want stopped containers hanging around
    remove = True

    # Default r2d images start jupyter notebook, not singleuser
    cmd = ['jupyterhub-singleuser']

    repo = Unicode(
        None,
        allow_none=True,
        config=True,
        help="""
        Repository to pass to repo2docker.

        Should not be None
        """
    )

    ref = Unicode(
        'master',
        config=True,
        help="""
        Ref to pass to repo2docker.
        """
    )

    async def inspect_image(self, image_spec):
        """
        Return docker image info if image exists, None otherwise
        """
        try:
            loop = IOLoop.current()
            # FIXME: Can't see to use self.docker here, fails with
            # `object Future can't be used in 'await' expression`.
            # So we reach into self.executor and self.client, which makes me nervous
            image_info = await loop.run_in_executor(self.executor, self.client.inspect_image, image_spec)
            return image_info
        except docker.errors.ImageNotFound:
            return None

    async def start(self):
        if self.repo is None:
            raise ValueError("Repo2DockerSpawner.repo must be set")
        resolved_ref = await resolve_ref(self.repo, self.ref)
        repo_escaped = escape(self.repo, escape_char='-').lower()
        image_spec = f'r2dspawner-{repo_escaped}:{resolved_ref}'
        image_info = await self.inspect_image(image_spec)
        if not image_info:
            self.log.info(f'Image {image_spec} not present, building...')
            r2d = Repo2Docker()
            r2d.repo = self.repo
            r2d.ref = resolved_ref
            r2d.user_id = 1000
            r2d.user_name = 'dspuser'

            r2d.output_image_spec = image_spec
            r2d.initialize()

            await self.run_in_executor(r2d.build)


        # HACK: DockerSpawner (and traitlets) don't seem to realize we're setting 'cmd',
        # and refuse to use our custom command. Explicitly set this variable for
        # now.
        self._user_set_cmd = True

        self.log.info(f'Launching with image {image_spec} for {self.user.name}')
        self.image = image_spec

        return await DSPSwarmSpawner.start(self)

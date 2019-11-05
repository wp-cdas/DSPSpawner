from setuptools import setup, find_packages

setup(
    name='jupyterhub-repo2dockerspawner',
    version='0.1',
    packages=find_packages(),
    install_requires=[
        'dockerspawner',
        'wpcdas-repo2docker @ git+https://github.com/wp-cdas/repo2docker.git'
    ],
)

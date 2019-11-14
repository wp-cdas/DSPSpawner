from setuptools import setup, find_packages

setup(
    name='wpcdas-dspspawner',
    version='0.1',
    packages=find_packages(),
    install_requires=[
        'dockerspawner',
        'wrapspawner @ git+https://github.com/jupyterhub/wrapspawner',
        'wpcdas-repo2docker @ git+https://github.com/wp-cdas/repo2docker.git'
    ],
)

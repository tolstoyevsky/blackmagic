from setuptools import setup

setup(name='blackmagic',
      version='0.1',
      description='',
      url='https://bitbucket.org/eugulixes/blackmagic',
      author='CusDeb Team',
      maintainer='Evgeny Golyshev',
      maintainer_email='Evgeny Golyshev <eugulixes@gmail.com>',
      license='http://www.apache.org/licenses/LICENSE-2.0',
      scripts=['bin/blackmagic.py'],
      install_requires=[
          'chardet',
          'pymongo',
          'python-debian'
      ])

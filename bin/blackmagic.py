#!/usr/bin/env python3
import logging
import os
import os.path
import re
import shutil
import subprocess
import tarfile
import time
import uuid
from functools import wraps

import configurations.management
# In spite of the fact that the above-mentioned import is never used throughout
# the code, the django.core.exceptions.ImproperlyConfigured exception will be
# raised if it's removed.
import django
import tornado.ioloop
import tornado.web
import tornado.websocket
from debian import deb822
from django.conf import settings
from pymongo import MongoClient
from tornado import gen
from tornado.options import define, options

from firmwares.models import Firmware
from shirow.server import RPCServer, remote
from users.models import User

define('base_system',
       default='/var/blackmagic/jessie-armhf',
       help='The path to a chroot environment which contains '
            'the Debian base system')
define('collection_name',
       default='jessie-armhf',
       help='')
define('db_name',
       default='cusdeb',
       help='')
define('keyring_package',
       default='/var/blackmagic/debian-archive-keyring_2014.3_all.deb',
       help='')
define('mongodb_host',
       default='localhost',
       help='')
define('mongodb_port',
       default=27017,
       help='')
define('status_file',
       default='/var/blackmagic/status',
       help='')
define('workspace',
       default='/var/blackmagic/workspace')

LOGGER = logging.getLogger('tornado.application')


def only_if_unlocked(func):
    """Executes a remote procedure only if the RPC server is unlocked. Every
    single remote procedure has to be decorated with only_if_unlocked. The
    exceptions are:
    * init
    * get_built_images
    * get_target_devices_list"""

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self.global_lock:
            return func(self, *args, **kwargs)
        else:
            return self._say_server_locked()

    return wrapper


class Application(tornado.web.Application):
    def __init__(self):
        handlers = [
            (r'/rpc/token/([_\-\w\.]+)', RPCHandler),
        ]
        tornado.web.Application.__init__(self, handlers)


class RPCHandler(RPCServer):
    base_packages_list = []
    users_list = []

    def __init__(self, application, request, **kwargs):
        RPCServer.__init__(self, application, request, **kwargs)

        self.inst_pattern = re.compile('Inst ([-\.\w]+)')

        self.user = None
        self.users = []

        self.build_lock = False
        self.global_lock = True
        self.init_lock = False
        self.lock_message = 'Locked'

        self.selected_packages = []

        self.firmware_name = str(uuid.uuid4())
        self.rootfs = os.path.join(options.workspace, self.firmware_name)

        client = MongoClient(options.mongodb_host, options.mongodb_port)
        self.db = client[options.db_name]
        self.collection = self.db[options.collection_name]

        self.packages_number = self.collection.find().count()

    def _get_user(self):
        if not self.user:
            self.user = User.objects.get(id=self.user_id)
            return self.user
        else:
            return self.user

    @gen.coroutine
    def _say_server_locked(self):
        return self.lock_message

    def destroy(self):
        if os.path.isdir(self.rootfs):
            LOGGER.debug('Remove {}'.format(self.rootfs))
            shutil.rmtree(self.rootfs)

    @remote
    def init(self, name, target_device, distro, distro_suite):
        if not self.init_lock:
            self.init_lock = True

            LOGGER.debug('Creating hierarchy in {}'.format(self.rootfs))
            hiera = [
                '/etc/apt',
                '/etc/apt/preferences.d',
                '/var/cache/apt/archives/partial',
                '/var/lib/apt/lists/partial',
                '/var/lib/dpkg',
            ]
            for directory in hiera:
                os.makedirs(self.rootfs + directory)

            shutil.copyfile(options.status_file,
                            self.rootfs + '/var/lib/dpkg/status')

            with open(self.rootfs + '/etc/apt/sources.list', 'w') as f:
                f.write('deb http://ftp.ru.debian.org/debian jessie main')

            command_line = ['dpkg', '-x', options.keyring_package, self.rootfs]
            proc = subprocess.Popen(command_line)
            proc.wait()

            LOGGER.debug('Executing apt-get update')

            command_line = [
                'apt-get', 'update', '-qq',
                '-o', 'APT::Architecture=all',
                '-o', 'APT::Architecture=armhf',
                '-o', 'Dir=' + self.rootfs,
                '-o', 'Dir::State::status=' + self.rootfs +
                      '/var/lib/dpkg/status'
            ]
            proc = subprocess.Popen(command_line)
            proc.wait()

            LOGGER.debug('Finishing initialization')

            self.init_lock = False
            self.global_lock = False

            return 'Ready'
        return self.lock_message

    @only_if_unlocked
    @remote
    def build(self):
        if not self.build_lock:
            self.build_lock = True

            if os.environ.get('DJANGO_CONFIGURATION', '') == 'Test':
                time.sleep(settings.PAUSE)
            else:
                if self.selected_packages:
                    command_line = ['chroot', self.rootfs,
                                    '/usr/bin/apt-get',
                                    'install',
                                    '--yes'] + self.selected_packages
                    proc = subprocess.Popen(command_line)
                    proc.wait()

                os.chdir(options.workspace)
                with tarfile.open(self.rootfs + '.tar.gz', 'w:gz') as tar:
                    tar.add(self.firmware_name)

                firmware = Firmware(name=self.firmware_name,
                                    user=self._get_user())
                firmware.save()

            self.build_lock = False

            return 'Ready'
        return self.lock_message

    @only_if_unlocked
    @remote
    def add_user(self, username, password, uid, gid, comment, homedir, shell):
        self.users.append({
            'username': username,
            'password': password,
            'uid': uid,
            'gid': gid,
            'comment': comment,
            'homedir': homedir,
            'shell': shell
        })
        return 'ok'

    @only_if_unlocked
    @remote
    def get_base_packages_list(self):
        return self.base_packages_list

    @remote
    def get_built_images(self):
        user = User.objects.get(id=self.user_id)
        firmwares = Firmware.objects.filter(user=user)
        return [firmware.name for firmware in firmwares]

    @only_if_unlocked
    @remote
    def get_packages_list(self, page_number, per_page):
        if page_number > 0:
            start_position = (page_number - 1) * per_page
        else:
            start_position = 0

        collection = self.collection
        packages_list = []
        for document in collection.find().skip(start_position).limit(per_page):
            # Originally _id is an ObjectId instance and it's not JSON
            # serializable
            document['_id'] = str(document['_id'])
            packages_list.append(document)

        return packages_list

    @only_if_unlocked
    @remote
    def get_shells_list(self):
        return ['/bin/sh', '/bin/dash', '/bin/bash', '/bin/rbash']

    @remote
    def get_target_devices_list(self):
        target_devices_list = [
            'Raspberry Pi 2',
        ]
        return target_devices_list

    @only_if_unlocked
    @remote
    def get_packages_number(self):
        return self.packages_number

    @only_if_unlocked
    @remote
    def get_users_list(self):
        return self.users_list

    @only_if_unlocked
    @remote
    def search(self, query):
        packages_list = []
        if query:
            matches = self.db.command('text', options.collection_name,
                                      search=query)
            if matches['results']:
                for document in matches['results']:
                    document['obj'].pop('_id')
                    packages_list.append(document['obj'])

        return packages_list

    @only_if_unlocked
    @remote
    def resolve(self, packages_list):
        self.selected_packages = packages_list

        command_line = [
            'apt-get', 'install', '--no-act', '-qq',
            '-o', 'APT::Architecture=all',
            '-o', 'APT::Architecture=armhf',
            '-o', 'Dir=' + self.rootfs,
            '-o', 'Dir::State::status=' + self.rootfs + '/var/lib/dpkg/status'
        ] + packages_list

        apt_proc = subprocess.Popen(command_line,
                                    stdout=subprocess.PIPE,
                                    stdin=subprocess.PIPE)
        stdout_data, stderr_data = apt_proc.communicate()

        # The output of the above command line will look like the
        # following set of lines:
        # NOTE: This is only a simulation!
        #       apt-get needs root privileges for real execution.
        #       Keep also in mind that locking is deactivated,
        #       so don't depend on the relevance to the real current situation!
        # Inst libgdbm3 (1.8.3-13.1 Debian:8.4/stable [armhf])
        # Inst libssl1.0.0 (1.0.1k-3+deb8u4 Debian:8.4/stable [armhf])
        # Inst libxml2 (2.9.1+dfsg1-5+deb8u1 Debian:8.4/stable [armhf])
        # ...
        # Conf libgdbm3 (1.8.3-13.1 Debian:8.4/stable [armhf])
        # Conf libssl1.0.0 (1.0.1k-3+deb8u4 Debian:8.4/stable [armhf])
        # Conf libxml2 (2.9.1+dfsg1-5+deb8u1 Debian:8.4/stable [armhf])
        # ...
        packages_to_be_installed = self.inst_pattern.findall(str(stdout_data))
        dependencies = set(packages_to_be_installed) - set(packages_list)

        return list(dependencies)  # Python sets are not JSON serializable


def main():
    tornado.options.parse_command_line()
    if not os.path.isdir(options.base_system):
        LOGGER.error('The directory specified via the base_system parameter '
                     'does not exist')
        exit(1)

    if not os.path.isfile(options.keyring_package):
        LOGGER.error('The file specified via the keyring_package parameter '
                     'does not exist')
        exit(1)

    if not os.path.isfile(options.status_file):
        LOGGER.error('The file specified via the status_file parameter '
                     'does not exist')
        exit(1)

    if not os.path.isdir(options.workspace):
        LOGGER.error('The directory specified via the workspace parameter '
                     'does not exist')
        exit(1)

    app = Application()
    app.listen(options.port)

    django.setup()

    passwd_file = os.path.join(options.base_system, 'etc/passwd')
    status_file = os.path.join(options.base_system, 'var/lib/dpkg/status')

    with open(passwd_file, encoding='utf-8') as f:
        for line in f:
            RPCHandler.users_list.append(line.split(':'))

    with open(status_file, encoding='utf-8') as f:
        for package in deb822.Packages.iter_paragraphs(f):
            RPCHandler.base_packages_list.append(package['package'])

    LOGGER.info('RPC server is ready!')

    tornado.ioloop.IOLoop.instance().start()


if __name__ == "__main__":
    main()

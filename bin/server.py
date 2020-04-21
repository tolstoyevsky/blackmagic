#!/usr/bin/env python3
import logging
import os
import os.path
import re
import shutil
import subprocess
import uuid
import urllib.request
import json
from functools import wraps
from pathlib import Path

import django
import tornado.web
import tornado.options
from celery.result import AsyncResult
from debian import deb822
from django.conf import settings
from dominion.tasks import build
from pymongo import MongoClient
from shirow import util
from json import JSONDecodeError
from tornado import gen
from tornado.options import define, options
from tornado.process import Subprocess

from blackmagic import defaults
from blackmagic.decorators import only_if_initialized
from firmwares.models import Firmware, TargetDevice, Distro, UnknownBuildTypeId
from shirow.ioloop import IOLoop
from shirow.server import RPCServer, TOKEN_PATTERN, remote
from users.models import User

define('base_systems_path',
       default='/var/chroot',
       help='The path to the directory which contains chroot environments '
            'which, in turn, contain the Debian base system')
define('db_name',
       default=settings.MONGO['DATABASE'],
       help='')
define('dominion_workspace',
       default='/var/dominion/workspace/',
       help='')
define('max_builds_number',
       default=8,
       type=int,
       help='Maximum allowed number of builds at the same time.')
define('mongodb_host',
       default=settings.MONGO['HOST'],
       help='')
define('mongodb_port',
       default=settings.MONGO['PORT'],
       help='')

LOGGER = logging.getLogger('tornado.application')

METAS = {
    'Raspbian 10 "Buster" (32-bit)': [
        'raspbian-buster-armhf',
        'http://archive.raspbian.org/raspbian',
        ('http://archive.raspbian.org/raspbian/pool/main/r/raspbian-archive-'
         'keyring/raspbian-archive-keyring_20120528.2_all.deb'),
    ],
    'Ubuntu 16.04 "Xenial Xerus" (32-bit)': [
        'ubuntu-xenial-armhf',
        'http://ports.ubuntu.com/ubuntu-ports/',
        ('http://ports.ubuntu.com/ubuntu-ports/pool/main/u/ubuntu-keyring/'
         'ubuntu-keyring_2012.05.19_all.deb'),
    ],
    'Ubuntu 18.04 "Bionic Beaver" (32-bit)': [
        'ubuntu-bionic-armhf',
        'http://ports.ubuntu.com/ubuntu-ports/',
        ('http://ports.ubuntu.com/ubuntu-ports/pool/main/u/ubuntu-keyring/'
         'ubuntu-keyring_2018.02.28_all.deb'),
    ],
    'Ubuntu 18.04 "Bionic Beaver" (64-bit)': [
        'ubuntu-bionic-arm64',
        'http://ports.ubuntu.com/ubuntu-ports/',
        ('http://ports.ubuntu.com/ubuntu-ports/pool/main/u/ubuntu-keyring/'
         'ubuntu-keyring_2018.02.28_all.deb'),
    ],
    'Devuan 1 "Jessie" (32-bit)': [
        'devuan-jessie-armhf',
        'http://auto.mirror.devuan.org/merged/',
        ('http://auto.mirror.devuan.org/merged/pool/DEVUAN/main/d/devuan-keyring/'
         'devuan-keyring_2017.10.03_all.deb'),
    ],
    'Debian 10 "Buster" (32-bit)': [
        'debian-buster-armhf',
        'http://deb.debian.org/debian/',
        ('http://deb.debian.org/debian/pool/main/d/debian-keyring/'
         'debian-keyring_2019.02.25_all.deb'),
    ],
}

READY = 10
BUSY = 12
LOCKED = 13
BUILD_FAILED = 18
EMAIL_NOTIFICATIONS = 19
EMAIL_NOTIFICATIONS_FAILED = 20
OVERLOADED = 21
FIRMWARE_WAS_REMOVED = 21
NOT_FOUND = 22
MAINTENANCE_MODE = 23
UNKNOWN_BUID_TYPE = 24


class DistroDoesNotExist(Exception):
    """Exception raised by the get_os_name function if the specified suite
    is not valid.
    """
    pass


def get_os_name(distro):
    if distro in METAS.keys():
        return METAS[distro][0]
    else:
        raise DistroDoesNotExist


def get_mirror_address(distro):
    if distro in METAS.keys():
        return METAS[distro][1]
    else:
        raise DistroDoesNotExist


def is_paid(distro, device):
    if device == 'Orange Pi Zero' and distro == 'Debian 10 "Buster" (32-bit)':
        return False
    else:
        return True


class Application(tornado.web.Application):
    def __init__(self):
        handlers = [
            (r'/rpc/token/' + TOKEN_PATTERN, RPCHandler),
        ]
        tornado.web.Application.__init__(self, handlers)


class RPCHandler(RPCServer):
    base_packages_list = {}
    users_list = {}
    for v in METAS.values():
        base_packages_list[v[0]] = []
        users_list[v[0]] = []

    def __init__(self, application, request, **kwargs):
        RPCServer.__init__(self, application, request, **kwargs)

        self.build_lock = False
        self.global_lock = True
        self.init_lock = False

        self._arch = ''
        self._collection_name = ''
        self._mirror = ''
        self._os = ''
        self._suite = ''
        self._paid = False

        self.image = {
            'id': None,
            'root_password': defaults.ROOT_PASSWORD,
            'selected_packages': [],
            'target': {},
            'users': [],
            'configuration': dict(defaults.CONFIGURATION),
        }
        self._distro = None
        self._target_device = None

        self.user = None  # the one who builds an image

    def _get_user(self):
        if not self.user:
            self.user = User.objects.get(id=self.user_id)
            return self.user
        else:
            return self.user

    def _get_distro(self, distro_name):
        if not self._distro:
            self._distro = Distro.objects.get(full_name=distro_name)
            return self._distro
        else:
            return self._distro

    def _get_target_device(self, target_device_name):
        if not self._target_device:
            self._target_device = TargetDevice.objects.get(full_name=target_device_name)
            return self._target_device
        else:
            return self._target_device

    def _init_mongodb(self):
        client = MongoClient(options.mongodb_host, options.mongodb_port)
        self.db = client[options.db_name]

    @remote
    def init(self, request, name, target_device_name, distro_name, build_type_id=1):
        if self.init_lock:
            request.ret(LOCKED)

        if self.build_lock:
            request.ret(BUSY)

        self.init_lock = True

        self._paid = is_paid(distro_name, target_device_name)
        self._os = get_os_name(distro_name)
        self._arch = self._os.split('-')[2]
        self._suite = self._os.split('-')[1]
        self._mirror = get_mirror_address(distro_name)
        self._collection_name = self._os
        self._init_mongodb()
        self.collection = self.db[self._collection_name]
        self.packages_number = self.collection.find().count()

        self.image['id'] = build_id = str(uuid.uuid4())
        self.image['_id'] = build_id

        self.image['target'] = {
            'distro': distro_name,
            'device': target_device_name
        }
        self.image['build_type'] = build_type_id

        user = self._get_user()
        distro = self._get_distro(distro_name)
        target_device = self._get_target_device(target_device_name)
        firmware = Firmware(name=build_id, user=user,
                            status=Firmware.INITIALIZED,
                            pro_only=self._paid,
                            distro=distro,
                            targetdevice=target_device)
        try:
            firmware.set_build_type(build_type_id)
        except UnknownBuildTypeId as e:
            LOGGER.error(str(e))
            request.ret(UNKNOWN_BUID_TYPE)
        else:
            firmware.save()

        LOGGER.debug('Finishing initialization')

        self.init_lock = False
        self.global_lock = False

        request.ret_and_continue(build_id)

        request.ret(READY)

    @only_if_initialized
    @remote
    def build(self, request):
        if not self.build_lock:
            self.build_lock = True

            ret_code = 0

            result = AsyncResult(build.delay(self.user_id, self.image))
            while not result.ready():
                yield gen.sleep(1)

            self.build_lock = False

            try:
                ret_code = result.get()
            except Exception:
                LOGGER.exception('An exception was raised while building '
                                 'image')
                request.ret(BUILD_FAILED)

            if ret_code == 0:
                request.ret(READY)
            else:
                request.ret(BUILD_FAILED)

        request.ret(LOCKED)

    @only_if_initialized
    @remote
    def add_user(self, request, username, password, uid, gid, comment, homedir,
                 shell):
        self.image['users'].append({
            'username': username,
            'password': password,
            'uid': uid,
            'gid': gid,
            'comment': comment,
            'homedir': homedir,
            'shell': shell
        })
        request.ret(READY)

    @only_if_initialized
    @remote
    def change_root_password(self, request, password):
        self.image['root_password'] = password
        request.ret(READY)

    @only_if_initialized
    @remote
    def sync_configuration(self, request, image_configuration_params):
        self.image['configuration'].update(image_configuration_params)
        request.ret(READY)

    @only_if_initialized
    @remote
    def get_default_configuration(self, request):
        request.ret(defaults.CONFIGURATION)

    @only_if_initialized
    @remote
    def get_base_packages_list(self, request):
        request.ret(self.base_packages_list[self._os])

    @only_if_initialized
    @remote
    def get_packages_list(self, request, page_number, per_page):
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

        request.ret(packages_list)

    @only_if_initialized
    @remote
    def get_default_root_password(self, request):
        request.ret(defaults.ROOT_PASSWORD)

    @only_if_initialized
    @remote
    def get_shells_list(self, request):
        request.ret(['/bin/sh', '/bin/dash', '/bin/bash', '/bin/rbash'])

    @only_if_initialized
    @remote
    def get_packages_number(self, request):
        request.ret(self.packages_number)

    @only_if_initialized
    @remote
    def get_users_list(self, request):
        request.ret(self.users_list[self._os])

    @only_if_initialized
    @remote
    def search(self, request, query):
        packages_list = []
        if query:
            matches = self.db.command('text', self._collection_name,
                                      search=query)
            if matches['results']:
                for document in matches['results']:
                    document['obj'].pop('_id')
                    packages_list.append(document['obj'])

        request.ret(packages_list)

    @only_if_initialized
    @remote
    def resolve(self, request, packages_list):
        LOGGER.debug(f'Resolve dependencies for {packages_list}')
        request.ret([])


def main():
    tornado.options.parse_command_line()
    if not os.path.isdir(options.base_systems_path):
        LOGGER.error('The directory specified via the base_systems_path '
                     'parameter does not exist')
        exit(1)

    django.setup()

    for v in METAS.values():
        passwd_file = os.path.join(options.base_systems_path, v[0], 'etc/passwd')

        with open(passwd_file, encoding='utf-8') as f:
            for line in f:
                RPCHandler.users_list[v[0]].append(line.split(':'))

    for v in METAS.values():
        base_sytem = os.path.join(options.base_systems_path, v[0])
        status_file = os.path.join(base_sytem, 'var/lib/dpkg/status')
        with open(status_file, encoding='utf-8') as f:
            for package in deb822.Packages.iter_paragraphs(f):
                RPCHandler.base_packages_list[v[0]].append(package['package'])

    LOGGER.info('RPC server is ready!')

    IOLoop().start(Application(), options.port)

if __name__ == "__main__":
    main()

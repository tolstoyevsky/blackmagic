#!/usr/bin/env python3
import logging
import os
import os.path
import re
import subprocess

import tornado.ioloop
import tornado.web
import tornado.websocket
from debian import deb822
from tornado.options import define, options

from shirow.server import RPCServer, remote

define('base_system',
       default='/var/blackmagic/jessie-armhf',
       help='The path to a chroot environment which contains '
            'the Debian base system')
define('packages_file',
       default='/var/blackmagic/Packages',
       help='The path to the Packages file')
define('workspace',
       default='/var/blackmagic/workspace')

LOGGER = logging.getLogger('tornado.application')


class Application(tornado.web.Application):
    def __init__(self):
        handlers = [
            (r'/rpc/token/([\w\.]+)', RPCHandler),
        ]
        tornado.web.Application.__init__(self, handlers)


class RPCHandler(RPCServer):
    base_packages_list = []
    packages_list = []
    packages_number = 0
    users_list = []

    def __init__(self, application, request, **kwargs):
        RPCServer.__init__(self, application, request, **kwargs)

        self.apt_lock = False

    @remote
    def get_base_packages_list(self):
        return self.base_packages_list

    @remote
    def get_dependencies_for(self, package_name):
        for package in self.packages_list:
            if package['package'] == package_name:
                dependencies_list = package['dependencies'] + ' '
                return re.findall('([-\w\d\.]+)[ ,]', dependencies_list)
        return []

    @remote
    def get_packages_list(self, page_number, amount):
        start_position = (page_number - 1) * amount
        return self.packages_list[start_position:start_position + amount]

    @remote
    def get_target_devices_list(self):
        target_devices_list = [
            'Raspberry Pi 2',
        ]
        return target_devices_list

    @remote
    def get_packages_number(self):
        return self.packages_number

    @remote
    def get_users_list(self):
        return self.users_list

    @remote
    def resolve(self, packages_list):
        if not self.apt_lock:
            self.apt_lock = True

            command = ['chroot', '../jessie-subsidiary', '/usr/bin/apt-get',
                       'install', '--no-act', '-qq'] + packages_list
            proc = subprocess.Popen(command,
                                    stdout=subprocess.PIPE,
                                    stdin=subprocess.PIPE)
            stdout_data, stderr_data = proc.communicate()
            res = re.findall('Inst ([-\w\d\.]+)', str(stdout_data))

            self.apt_lock = False

            return list(set(res) - set(packages_list))
        return 'Locked'


def main():
    tornado.options.parse_command_line()
    if os.getuid() > 0:
        LOGGER.error('The server can only be run by root')
        exit(1)

    if not os.path.isdir(options.base_system):
        LOGGER.error('The directory specified via the base_system parameter '
                     'does not exist')
        exit(1)

    if not os.path.isfile(options.packages_file):
        LOGGER.error('The Packages file specified via the packages_file '
                     'parameter does not exist')
        exit(1)

    if not os.path.isdir(options.workspace):
        LOGGER.error('The directory specified via the workspace parameter '
                     'does not exist')
        exit(1)

    app = Application()
    app.listen(options.port)

    passwd_file = os.path.join(options.base_system, 'etc/passwd')
    status_file = os.path.join(options.base_system, 'var/lib/dpkg/status')

    with open(options.packages_file) as f:
        for package in deb822.Sources.iter_paragraphs(f):
            description_lines = package['description']

            RPCHandler.packages_list.append({
                'package': package['package'],
                'dependencies': package.get('depends', ''),
                'description': description_lines,
                'version': package['version'],
                'size': package['size'],
                'type': ''
            })

    RPCHandler.packages_number = len(RPCHandler.packages_list)

    with open(status_file) as f:
        for package in deb822.Sources.iter_paragraphs(f):
            RPCHandler.base_packages_list.append(package['package'])

    with open(passwd_file) as f:
        for line in f:
            RPCHandler.users_list.append(line.split(':'))

    LOGGER.info('RPC server is ready!')
    tornado.ioloop.IOLoop.instance().start()


if __name__ == "__main__":
    main()

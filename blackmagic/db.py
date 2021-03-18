# Copyright 2020 Evgeny Golyshev. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import uuid

from channels.db import database_sync_to_async
from django.contrib.auth.models import User

from blackmagic import defaults
from blackmagic.exceptions import RecoveryImageIsMissing
from images.models import Image as ImageModel


class Image:
    """Class representing an image. """

    def __init__(self, image_id=None, user_id=None, device_name=None, distro_name=None,
                 flavour=None):
        self._image = None
        self._user_id = user_id
        self._device_name = device_name
        self._flavour = flavour
        self._status = ImageModel.UNDEFINED
        self.distro_name = distro_name
        self.selected_packages = []
        self.configuration = {}
        self.pieman_user = None
        self.root_password = defaults.ROOT_PASSWORD

        self.image_id = image_id or str(uuid.uuid4())

        if image_id:
            try:
                self._image = ImageModel.objects.get(image_id=image_id)
            except ImageModel.DoesNotExist as exc:
                raise RecoveryImageIsMissing from exc
            else:
                self.distro_name = self._image.distro_name

                pieman_user_username = self._image.props.get('PIEMAN_USER_NAME')
                pieman_user_password = self._image.props.get('PIEMAN_USER_PASSWORD')
                if pieman_user_username:
                    self.pieman_user = {
                        'username': pieman_user_username,
                        'password': pieman_user_password,
                    }

                root_password = self._image.props.get('PIEMAN_PASSWORD')
                if root_password:
                    self.root_password = root_password

                pieman_includes = self._image.props['PIEMAN_INCLUDES']
                if pieman_includes:
                    self.selected_packages = pieman_includes.split(',')

                self.configuration = dict(defaults.CONFIGURATION)
                for conf_key, conf_value in self.configuration.items():
                    prop_value = self._image.props.get(f'PIEMAN_{conf_key.upper()}')
                    if prop_value:
                        self.configuration[conf_key] = prop_value

    def _get_configuration_props(self):
        props = {}
        for prop_key, prop_value in self.configuration.items():
            if (not self.configuration['enable_wireless']
                    and prop_key in defaults.WIRELESS_CONFIGURATION_KEYS):
                continue

            prop_name = f'PIEMAN_{prop_key.upper()}'
            props[prop_name] = prop_value

        return props

    @staticmethod
    def _serialize_props(props):
        for prop_key, prop_value in props.items():
            if isinstance(prop_value, bool):
                props[prop_key] = str(prop_value).lower()

    def enqueue(self):
        """Changes the image status to PENDING. """

        self._status = ImageModel.PENDING

    def dump_sync(self):
        image = self._image or ImageModel()

        if not self._image:
            try:
                image.user = User.objects.get(pk=self._user_id)
            except User.DoesNotExist:
                """User doesn't exist. """

            image.image_id = self.image_id
            image.device_name = self._device_name
            image.distro_name = self.distro_name
            image.flavour = 'C'

        image.status = self._status
        configuration_props = self._get_configuration_props()
        props = {
            'PIEMAN_INCLUDES': ','.join(self.selected_packages),
            **configuration_props,
        }

        if self.pieman_user:
            props.update({
                'PIEMAN_USER_NAME': self.pieman_user['username'],
                'PIEMAN_USER_PASSWORD': self.pieman_user['password'],
            })

        props['PIEMAN_PASSWORD'] = self.root_password

        self._serialize_props(props)
        image.props = props

        image.save()

    @database_sync_to_async
    def dump(self):
        self.dump_sync()

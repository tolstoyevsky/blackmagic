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

HOST_NAME = 'cusdeb'

ENABLE_WIRELESS = False

WPA_SSID = 'cusdeb'

WPA_PSK = ''

TIME_ZONE = 'Etc/UTC'

CONFIGURATION = {
    'host_name': HOST_NAME,
    'time_zone': TIME_ZONE,
    'enable_wireless': ENABLE_WIRELESS,
    'WPA_SSID': WPA_SSID,
    'WPA_PSK': WPA_PSK,
}

WIRELESS_CONFIGURATION_KEYS = ['enable_wireless', 'WPA_SSID', 'WPA_PSK']

ROOT_PASSWORD = 'cusdeb'

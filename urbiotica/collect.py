#!/usr/bin/env python
# pylint: disable=line-too-long
"""Load ParkingSpot data from Urbiotica API"""

import itertools
import math
import logging
import sys
import traceback
import json
import time

from datetime import datetime, timedelta, timezone
from operator import itemgetter
from typing import Dict, Any, List, Generator, Optional, Protocol, Sequence
from collections import defaultdict
from dataclasses import dataclass

import requests
import urllib3 # type: ignore
import configargparse # type: ignore

from dateutil import parser # type: ignore
from limiter import Limiter, get_limiter, limit_rate # type: ignore
from shapely.geometry import Polygon # type: ignore


# -------------------
# Orion-related stuff
# -------------------

class Store(Protocol):
    """Store represents any persistence backend"""
    def open(self):
        """Ready the store for saving data"""
    def send_batch(self, subservice: str, entities: Sequence[Any]) -> None:
        """Saves a batch of entities to the backend"""
    def close(self):
        """Closes the backend connection"""

# pylint: disable=redefined-outer-name
class Session(Protocol):
    """Session represents a requests.Session"""
    def get(self, url: str, headers: Optional[Dict[str, str]]=None, params: Optional[Dict[str, str]]=None, verify: Optional[bool]=None) -> requests.Response:
        """Performs an http GET"""
    def post(self, url: str, headers: Optional[Dict[str, str]]=None, json: Any=None, verify: Optional[bool]=None) -> requests.Response:
        """Perform an HTTP POST"""


# Define classes
@dataclass(frozen=True)
class CustomException(Exception):
    """Exception raised for errors in the methods.

    Attributes:
        msg  -- explanation of the error
    """
    msg: str


@dataclass(frozen=True)
class NetworkException(Exception):
    """Exception raised for network errors.

    Attributes:
        msg: the failure message
        url -- URL the request was sent to
        status_code -- response status code
        text -- response body
    """
    msg: str
    url: str
    status_code: int
    text: str


# pylint: disable=too-many-instance-attributes,missing-function-docstring
@dataclass
class OrionStore:
    """Orion-based store"""
    endpoint_keystone: str
    endpoint_cb: str
    user: str
    password: str
    service: str
    seconds_sleep: int
    retries: int
    session: Session
    token: Dict[str, str]

    def open(self):
        """Open the store. For OrionStore, it's just a no-op"""

    def close(self):
        """Close the store. For OrionStore, it's a no-op"""

    def get_auth_token_subservice(self, subservice: str):
        """Get new authentication token from credentials"""
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

        body = {
            "auth": {
                "scope": {
                    "project": {
                        "domain": {
                            "name": self.service
                        },
                        "name": subservice
                    }
                },
                "identity": {
                    "password": {
                        "user": {
                            "domain": {
                                "name": self.service
                            },
                            "password": self.password,
                            "name": self.user
                        }
                    },
                    "methods": [
                        "password"
                    ]
                }
            }
        }

        logging.info('getting auth token (subservice "%s")...', subservice)
        req_url = self.endpoint_keystone + '/v3/auth/tokens'
        res = self.session.post(req_url, json=body, headers=headers, verify=False)

        if res.status_code != 201:
            logging.error('Failed to get auth token (subservice "%s") (%d) (%s)', subservice, res.status_code, res.text)
            raise NetworkException(msg='Failed to get auth token', url=req_url, status_code=res.status_code, text=res.text)

        self.token[subservice] = res.headers["X-Subject-Token"]
        logging.info('Authentication token for subservice "%s" was created successfully', subservice)

    def renew_token(self, subservice: str):
        headers = {
            'Content-Type': 'application/json'
        }
        body = {
            "auth": {
                "identity": {
                    "methods": [
                        "token"
                    ],
                    "token": {
                        "id": self.token[subservice]
                    }
                }
            }
        }

        logging.info('renewing token (subservice "%s")...', subservice)
        req_url = self.endpoint_keystone + '/v3/auth/tokens'
        res = self.session.post(req_url, json=body, headers=headers, verify=False)

        if res.status_code != 201:
            logging.error('Failed to renew token (subservice "%s") (%d) (%s)', subservice, res.status_code, res.text)
            raise NetworkException(msg='Failed to renew toen', url=req_url, status_code=res.status_code, text=res.text)

        self.token[subservice] = res.headers["X-Subject-Token"]
        logging.info('Authentication token for subservice "%s" was renewed successfully', subservice)

    def batch_url(self):
        """URL for batch requests to orion"""
        return self.endpoint_cb + '/v2/op/update'

    def batch_creation_update(self, subservice: str, entities: Sequence[Any]):
        """
        Send a POST /v2/op/update batch
        :param entities: the entities to be included in the batch (up to page_size, but construction)
        :return: response
        """
        headers = {
            'Fiware-Service': self.service,
            'Fiware-ServicePath': subservice,
            'X-Auth-Token': self.token[subservice],
            'Content-Type': 'application/json'
        }

        body = {
            'actionType': 'append',
            'entities': entities
        }

        req_url = self.batch_url()
        return self.session.post(req_url, json=body, headers=headers, verify=False)

    def send_batch(self, subservice: str, entities: Sequence[Any]):
        """
        Send a POST /v2/op/update batch
        :param entities: the entities to be included in the batch (up to page_size, but construction)
        :return: True if update was ok, False otherwise
        """
        logging.info('Subservice: "%s", %d entities', subservice, len(entities))
        if subservice not in self.token.keys():
            self.get_auth_token_subservice(subservice)

        done, retries = False, self.retries
        while not done:
            res = self.batch_creation_update(subservice, entities)
            if res.status_code == 401:
                self.renew_token(subservice)
                res = self.batch_creation_update(subservice, entities)

            if res.status_code == 204:
                done = True
            else:
                logging.error('Error in batch operation (%d): %s', res.status_code, res.text)
                if retries < 0:
                    raise NetworkException(msg='Error in batch operation', url=self.batch_url(), status_code=res.status_code, text=res.text)
                retries -= 1
                time.sleep(self.seconds_sleep)

        logging.info('Update batch of %d entities', len(entities))
        time.sleep(self.seconds_sleep)

    def get_url(self, entityid: str) -> str:
        return self.endpoint_cb + '/v2/entities/' + entityid

    def get_entity(self, subservice: str, entityid: str, entitytype: str) -> Any:
        """Get an entity by ID and type"""
        logging.info('GET entity %s subservice: "%s"', entityid, subservice)
        if subservice not in self.token.keys():
            self.get_auth_token_subservice(subservice)

        req_url = self.get_url(entityid)
        headers = {
            'Fiware-Service': self.service,
            'Fiware-ServicePath': subservice,
            'X-Auth-Token': self.token[subservice]
        }
        params = {"type": entitytype}
        retries = self.retries
        while True:
            res = self.session.get(req_url, headers=headers, params=params, verify=False)
            if res.status_code == 401:
                self.renew_token(subservice)
                res = self.session.get(req_url, headers=headers, params=params, verify=False)

            if res.status_code == 200:
                return res.json()

            logging.error('Error in get operation (%d): %s', res.status_code, res.text)
            if retries < 0:
                raise NetworkException(msg='Error in get operation', url=req_url, status_code=res.status_code, text=res.text)
            retries -= 1
            time.sleep(self.seconds_sleep)

# ---------------
# Urbiotica stuff
# ---------------

JsonDict = Dict[str, Any]
JsonList = List[JsonDict]


@dataclass
class Api:
    """Encapsulates top level API calls to urbiotica API"""

    endpoint: str
    organism: str
    token: str
    bucket: Limiter
    session: Session

    # pylint: disable=too-many-arguments
    @classmethod
    def login(cls, session: Session, endpoint: str, organism: str,
              username: str, password: str):
        """login with the provided credentials"""
        # API is rate limited to 100 requests per minute
        bucket = get_limiter(rate=100.0 / 60.0, capacity=100)
        with limit_rate(bucket):
            auth = session.get(
                f'{endpoint}/v2/auth/{organism}/{username}/{password}')
        if auth is None:
            raise ValueError('Invalid auth endpoint')
        logging.info("Authentication successful")
        return cls(endpoint, organism, auth.text.strip('"'), bucket, session)

    def projects(self) -> Dict[str, 'Project']:
        """projects associated to the logged-in user"""
        url = f'{self.endpoint}/v2/organisms/{self.organism}/projects'
        with limit_rate(self.bucket):
            prj = self.session.get(url, headers={'IDENTITY_KEY': self.token})
        if prj is None:
            raise ValueError("Invalid projects endpoint")
        logging.debug("Received project list: %s", prj.text)
        return {
            item['projectid']: Project.new(self, item)
            for item in prj.json()
        }

    # pylint: disable=too-many-arguments
    def query_project(self, projectid: str, path: str,
                      attrib: str) -> JsonDict:
        """query some sub-path for a particular project, use attrib as key in returned dict"""
        url = f'{self.endpoint}/v2/organisms/{self.organism}/projects/{projectid}/{path}'
        with limit_rate(self.bucket):
            its = self.session.get(url, headers={'IDENTITY_KEY': self.token})
        if its is None:
            raise ValueError("Invalid query endpoint")
        logging.debug("Received %s info for project %s: %s", path, projectid, its.text)
        return {item[attrib]: item for item in its.json()}


@dataclass
class Project:
    """Encapsulates project API"""

    api: Api
    projectid: str
    name: str
    description: str
    timezone: str

    @classmethod
    def new(cls, api: Api, project: JsonDict) -> 'Project':
        """New project from plain json project description"""
        return cls(api, project['projectid'], project['name'],
                   project['description'], project['timezone'])

    def parkings(self) -> JsonDict:
        """Enumerate project parkings"""
        return self.api.query_project(self.projectid, 'parkings', 'pomid')

    def zones(self) -> JsonDict:
        """Enumerate project zones"""
        return self.api.query_project(self.projectid, 'zones', 'zoneid')

    def spots(self) -> JsonDict:
        """Enumerate project spots"""
        return self.api.query_project(self.projectid, 'spots', 'pomid')

    def devices(self, zoneid: str) -> JsonDict:
        """Enumerate zone devices"""
        return self.api.query_project(self.projectid, f'zones/{zoneid}/devices', 'elementid')

    def rotations(self, pomid: str, from_dt: datetime,
                  to_dt: datetime) -> JsonList:
        """Enumerate spot rotations"""
        fromiso = datetime.isoformat(from_dt.replace(microsecond=0))
        toiso = datetime.isoformat(to_dt.replace(microsecond=0))
        poms = self.api.query_project(
            self.projectid,
            f'spots/{pomid}/rotations/finished/{fromiso}/{toiso}', 'pomid')
        rotations = list(
            itertools.chain(*(({
                'pomid': pom['pomid'],
                'start': parser.isoparse(item['start']),
                'end': parser.isoparse(item['end'])
            } for item in pom['rotations']) for pom in poms.values())))
        return Project._sortby(rotations, 'start')

    def vehicles(self, pomid: str, from_dt: datetime,
                 to_dt: datetime) -> JsonList:
        """Enumerate spot vehicle_ctrl events"""
        from_ts = math.floor(from_dt.timestamp())
        to_ts = math.ceil(to_dt.timestamp())
        if to_ts - from_ts > 7*24*60*60:
            to_ts = from_ts + 7*24*60*60
        try:
            poms = self.api.query_project(
                self.projectid,
                f'spots/{pomid}/phenomenons/vehicle_ctrl?start={from_ts}&end={to_ts}',
                'pomid')
        except requests.exceptions.RequestException as err:
            logging.error("Failed to fetch vehicles data: %s", err)
            try:
                poms = self.api.query_project(
                    self.projectid,
                    f'spots/{pomid}/phenomenons/vehicle_ctrl?start={from_ts}&end={to_ts}',
                    'pomid')
            except requests.exceptions.RequestException as err:
                logging.error("Retry failed, giving up on vehicles: %s", err)
                return []
        measurements = list(
            itertools.chain(*(({
                'pomid':
                pom['pomid'],
                'lstamp':
                datetime.fromtimestamp(int(item['lstamp']) // 1000,
                                       tz=timezone.utc),
                'value':
                item['value']
            } for item in pom['measurements']) for pom in poms.values())))
        return Project._sortby(measurements, 'lstamp')

    @staticmethod
    def _sortby(items: JsonList, field: str) -> JsonList:
        """Sort list by item attrib"""
        items.sort(key=itemgetter(field))
        return items


@dataclass
class SpotIterator:
    """Read vehicle information from Spot and turn into ParkingSpot entity sequence"""
    # IDs of POM, device and zone
    pomid: int
    deviceid: str
    zoneid: str

    # Orion entity IDs
    entityid: str
    deviceentityid: str
    zoneentityid: str

    # Other attributes of spot
    name: str
    coords: List[float]

    # Time range and events in that range
    from_ts: datetime
    to_ts: datetime
    events: JsonList

    # pylint: disable=too-many-arguments,too-many-locals
    @classmethod
    def collect(cls, project: Project,
                orion_cb: OrionStore, subservice: str, pom: JsonDict, device: JsonDict,
                to_ts: datetime):
        """Collect vehicle_ctrl events for the given pomid between most recent update, and to_ts"""
        pomid = pom['pomid']
        name = pom['name']
        logging.info("Collecting vehicle_ctrl events from pom %s (id %d)",
                     name, pomid)
        deviceid = device['elementid']
        zoneid = device['zoneid']
        coords = [float(pom['latitude']), float(pom['longitude'])]
        entityid = f'pomid:{pomid}'
        deviceentityid = f'elementid:{deviceid}'
        zoneentityid = f'zoneid:{zoneid}'
        logging.info('Getting latest occupancyModified for entity %s',
                     entityid)
        entity = orion_cb.get_entity(subservice=subservice, entityid=entityid, entitytype="ParkingSpot")
        from_ts = to_ts - timedelta(days=1)
        if entity is not None and 'occupancyModified' in entity:
            from_ts = parser.isoparse(entity['occupancyModified']['value'])
        logging.info('Getting events for pomid %d between %s and %s', pomid,
                     from_ts, to_ts)
        events = project.vehicles(pomid, from_ts, to_ts)
        return cls(pomid=pomid,
                   name=name,
                   deviceid=deviceid,
                   zoneid=zoneid,
                   coords=coords,
                   entityid=entityid,
                   deviceentityid=deviceentityid,
                   zoneentityid=zoneentityid,
                   from_ts=from_ts,
                   to_ts=to_ts,
                   events=events)

    def __iter__(self) -> Generator[JsonDict, None, None]:
        """Iterate on vehicle_ctrl events generating ParkingSpot entity updates"""
        if self.events:
            for event in self.events:
                timeinstant = event['lstamp'].isoformat()
                occupied = int(event['value'])
                yield {
                    'id': self.entityid,
                    'type': 'ParkingSpot',
                    'TimeInstant': {
                        'type': 'DateTime',
                        'value': timeinstant,
                    },
                    'occupancyModified': {
                        'type': 'DateTime',
                        'value': timeinstant,
                    },
                    'name': {
                        'type': 'Text',
                        'value': self.name
                    },
                    'status': {
                        'type':
                        'Text',
                        'value':
                        'free' if occupied == 0 else
                        ('occupied' if occupied == 1 else 'unknown'),
                    },
                    'refOnStreetParking': {
                        'type': 'Text',
                        'value': self.zoneentityid
                    },
                    'refDevice': {
                        'type': 'Text',
                        'value': self.deviceentityid
                    },
                    'location': {
                        'type': 'geo:json',
                        'value': {
                            'type': 'Point',
                            # HACK: Urbo coordinate system is "swapped"
                            'coordinates': [self.coords[1], self.coords[0]]
                        }
                    },
                    'occupied': {
                        'type': 'Number',
                        'value': occupied if occupied >= 0 else None
                    }
                }


def zone_to_entity(zone: JsonDict, zone_poms: JsonList, timeinstant: str):
    """Turn Zone information into OnStreetParking entity"""
    zoneid = zone['zoneid']
    name = zone['description']
    location = [(float(zone['lat_ne']) + float(zone['lat_sw'])) / 2,
                (float(zone['long_ne']) + float(zone['long_sw'])) / 2]
    points = [[float(pom['latitude']),
               float(pom['longitude'])] for pom in zone_poms]
    area = Polygon(points).buffer(
        0.0001).minimum_rotated_rectangle.exterior.coords
    return {
        'id': f'zoneid:{zoneid}',
        'type': 'OnStreetParking',
        'TimeInstant': {
            'type': 'DateTime',
            'value': timeinstant
        },
        'name': {
            'type': 'Text',
            'value': name
        },
        'location': {
            'type': 'geo:json',
            'value': {
                'type': 'Point',
                # HACK: Urbo swaps latitude and longitude...
                'coordinates': [location[1], location[0]]
            }
        },
        'polygon': {
            'type': 'geox:json',
            'value': {
                'type': 'Polygon',
                # HACK: Urbo swaps latitude and longitude...
                'coordinates': [[[item[1], item[0]] for item in area]]
            }
        },
        'totalSpotNumber': {
            'type': 'Number',
            'value': len(zone_poms)
        }
    }


# pylint: disable=too-many-locals
def main():
    """Main ETL function"""
    argparser = configargparse.ArgParser(default_config_files=['urbiotica.ini'])
    argparser.add('-c',
               '--config',
               required=False,
               is_config_file=True,
               env_var='CONFIG_FILE',
               help='config file path')
    argparser.add('--api-url',
               required=False,
               help='Urbiotica API URL',
               env_var='API_URL',
               default='http://api.urbiotica.net')
    argparser.add('--api-organism',
               required=True,
               help='Organism ID for urbiotica API',
               env_var='API_ORGANISM')
    argparser.add('--api-username',
               required=True,
               help='Username for urbiotica API',
               env_var='API_USERNAME')
    argparser.add('--api-password',
               required=True,
               help='Password for urbiotica API',
               env_var='API_PASSWORD')
    argparser.add('--keystone-url',
               required=False,
               help='Keystone URL',
               env_var='KEYSTONE_URL',
               default="https://auth.iotplatform.telefonica.com:15001")
    argparser.add('--orion-url',
               required=False,
               help='Orion URL',
               env_var='ORION_URL',
               default="https://cb.iotplatform.telefonica.com:10027")
    argparser.add('--orion-service',
               required=True,
               help='Orion service name',
               env_var="ORION_SERVICE")
    argparser.add('--orion-subservice',
               required=True,
               help='Orion subservice name',
               env_var="ORION_SUBSERVICE")
    argparser.add('--orion-username',
               required=True,
               help='Orion username',
               env_var="ORION_USERNAME")
    argparser.add('--orion-password',
               required=True,
               help='Orion password',
               env_var="ORION_PASSWORD")
    argparser.add('--orion-retries',
               required=False,
               default=0,
               type=int,
               choices=range(0, 6),
               env_var="ORION_RETRIES")
    argparser.add('--orion-sleep',
               required=False,
               default=1,
               type=int,
               choices=range(1, 100),
               help='Orion sleep between batches',
               env_var="ORION_SLEEP")
    argparser.add('--load-zones',
                required=False,
                help='load zones (OnStreetParkings) besides POMs (ParkingSpots)',
                dest='load_zones',
                action='store_true',
                default=False,
                env_var="LOAD_ZONES")
    options = argparser.parse_args()

    logging.info("Authenticating to url %s, service %s, username %s",
                 options.keystone_url, options.orion_service,
                 options.orion_username)

    orion_cb = OrionStore(
        endpoint_keystone=options.keystone_url,
        endpoint_cb=options.orion_url,
        service=options.orion_service,
        user=options.orion_username,
        password=options.orion_password,
        seconds_sleep=options.orion_sleep,
        retries=options.orion_retries,
        session=requests.Session(),
        token=dict())
    orion_cb.open()
    api = Api.login(requests.Session(), options.api_url, options.api_organism,
                    options.api_username, options.api_password)

    all_zones = dict()
    poms_by_zone = defaultdict(list)
    pom_params = list()
    now_ts = datetime.now()

    for project in api.projects().values():
        zones = project.zones()
        all_zones.update(zones)
        devices = dict()
        for zoneid in zones.keys():
            devices.update(project.devices(zoneid))
        for pom in project.spots().values():
            # Some projects have POMs without element IDs, probably errors.
            elementid = pom.get('elementid', '')
            if elementid == '':
                logging.warning("Found POM without ElementID: %s", json.dumps(pom))
                continue
            poms_by_zone[devices[elementid]['zoneid']].append(pom)
            pom_params.append({
                'project': project,
                'orion_cb': orion_cb,
                'subservice': options.orion_subservice,
                'pom': pom,
                'device': devices[pom['elementid']],
                'to_ts': now_ts,
            })

    batchSize = 20
    for pom in pom_params:
        entities = list(SpotIterator.collect(**pom))
        for base in range(0, len(entities), batchSize):
            logging.info(f'sending batch {base} to {base+batchSize}')
            orion_cb.send_batch(options.orion_subservice, entities[base:base+batchSize])

    if options.load_zones:
        logging.info("Loading zones")
        entities = list()
        timeinstant = datetime.utcnow().isoformat()
        for zoneid, zone in all_zones.items():
            zone_poms = poms_by_zone[zoneid]
            entities.append(zone_to_entity(zone, zone_poms, timeinstant))
        orion_cb.send_batch(options.orion_subservice, entities)



if __name__ == "__main__":

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler()
        ]
    )

    try:
        main()
        print("ETL OK")
    # pylint: disable=broad-except
    except Exception as err:
        print("ETL KO: ", err)
        traceback.print_exc()
        sys.exit(-1)

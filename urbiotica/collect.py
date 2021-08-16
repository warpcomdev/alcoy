#!/usr/bin/env python
"""Load ParkingSpot data from Urbiotica API"""

from datetime import datetime, timedelta, timezone
from operator import itemgetter
from typing import Dict, Any, List, Generator, Iterable, Iterator, Optional
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import itertools
import math
import logging
import sys
import traceback

import attr
from dotenv import dotenv_values
from limiter import Limiter, get_limiter, limit_rate
from shapely.geometry import Polygon
from orion import Session, ContextBroker

JsonDict = Dict[str, Any]
JsonList = List[JsonDict]


@attr.s(auto_attribs=True)
class Api:
    """Encapsulates top level API calls to urbiotica API"""

    endpoint: str
    organism: str
    token: str
    bucket: Limiter

    # pylint: disable=too-many-arguments
    @classmethod
    def login(cls, session: Session, endpoint: str, organism: str,
              username: str, password: str):
        """login with the provided credentials"""
        # API is rate limited to 100 requests per minute
        bucket = get_limiter(rate=100.0 / 60.0, capacity=100)
        with limit_rate(bucket):
            auth = session.get(
                f'{endpoint}/auth/{organism}/{username}/{password}')
        if auth.status_code != 200:
            raise ValueError('Invalid username of password')
        return cls(endpoint, organism, auth.text.strip('"'), bucket)

    def projects(self, session: Session) -> Dict[str, 'Project']:
        """projects associated to the logged-in user"""
        url = f'{self.endpoint}/organisms/{self.organism}/projects'
        with limit_rate(self.bucket):
            prj = session.get(url, headers={'IDENTITY_KEY': self.token})
        if prj.status_code != 200:
            raise ValueError(
                f'Failed to retrieve projects for organism {self.organism}')
        return {
            item['projectid']: Project.new(self, item)
            for item in prj.json()
        }

    # pylint: disable=too-many-arguments
    def query_project(self,
                      session: Session,
                      projectid: str,
                      path: str,
                      attrib: str,
                      debug=False) -> JsonDict:
        """query some sub-path for a particular project, use attrib as key in returned dict"""
        url = f'{self.endpoint}/organisms/{self.organism}/projects/{projectid}/{path}'
        with limit_rate(self.bucket):
            its = session.get(url, headers={'IDENTITY_KEY': self.token})
        if its.status_code != 200:
            raise ValueError(
                f'Failed to retrieve {path} for project {projectid}')
        if debug:
            print("JSON: ", its.json())
        return {item[attrib]: item for item in its.json()}


@attr.s(auto_attribs=True)
class Project:
    """Encapsulates project API"""

    api: Api
    projectid: str
    name: str
    description: str
    timezone: str

    @classmethod
    def new(cls, api: Api, project: JsonDict):
        """New project from plain json project description"""
        return cls(api, project['projectid'], project['name'],
                   project['description'], project['timezone'])

    def parkings(self, session: Session) -> JsonDict:
        """Enumerate project parkings"""
        return self.api.query_project(session, self.projectid, 'parkings',
                                      'pomid')

    def zones(self, session: Session) -> JsonDict:
        """Enumerate project zones"""
        return self.api.query_project(session, self.projectid, 'zones',
                                      'zoneid')

    def spots(self, session: Session) -> JsonDict:
        """Enumerate project spots"""
        return self.api.query_project(session, self.projectid, 'spots',
                                      'pomid')

    def devices(self, session: Session, zoneid: str) -> JsonDict:
        """Enumerate zone devices"""
        return self.api.query_project(session, self.projectid,
                                      f'zones/{zoneid}/devices', 'elementid')

    def rotations(self, session: Session, pomid: str, from_dt: datetime,
                  to_dt: datetime) -> JsonList:
        """Enumerate spot rotations"""
        fromiso = datetime.isoformat(from_dt.replace(microsecond=0))
        toiso = datetime.isoformat(to_dt.replace(microsecond=0))
        poms = self.api.query_project(
            session, self.projectid,
            f'spots/{pomid}/rotations/finished/{fromiso}/{toiso}', 'pomid')
        rotations = list(
            itertools.chain(*(({
                'pomid': pom['pomid'],
                'start': datetime.fromisoformat(item['start']),
                'end': datetime.fromisoformat(item['end'])
            } for item in pom['rotations']) for pom in poms.values())))
        return Project._sortby(rotations, 'start')

    def vehicles(self, session: Session, pomid: str, from_dt: datetime,
                 to_dt: datetime) -> JsonList:
        """Enumerate stop vehicle events"""
        from_ts = math.floor(from_dt.timestamp())
        to_ts = math.ceil(to_dt.timestamp()) + 1
        poms = self.api.query_project(
            session, self.projectid,
            f'spots/{pomid}/phenomenons/vehicle_ctrl?start={from_ts}&end={to_ts}',
            'pomid')
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


@attr.s(auto_attribs=True)
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
    def collect(cls, session: Session, project: Project,
                orion_cb: ContextBroker, pom: JsonDict, device: JsonDict,
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
        entity = orion_cb.get(session,
                              entityID=entityid,
                              entityType="ParkingSpot")
        from_ts = to_ts - timedelta(days=1)
        if entity is not None and 'occupancyModified' in entity:
            from_ts = datetime.fromisoformat(
                entity['occupancyModified']['value'].replace('Z', '+00:00'))
        logging.info('Getting events for pomid %d between %s and %s', pomid,
                     from_ts, to_ts)
        events = project.vehicles(session, pomid, from_ts, to_ts)
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
                        'value': occupied
                    }
                }


def zone_to_entity(zone: JsonDict, zone_poms: JsonList):
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
                'coordinates': [[item[1], item[0]] for item in area]
            }
        },
        'totalSpotNumber': {
            'type': 'Number',
            'value': len(zone_poms)
        }
    }


def rotate(
        iterators: List[Iterable[JsonDict]]
) -> Generator[JsonDict, None, None]:
    """Rotate a set of iterators yielding one item from each"""
    iterables: List[Optional[Iterator[JsonDict]]] = [
        iter(item) for item in iterators
    ]
    while len(iterables) > 0:
        depleted = False
        for index, item in enumerate(iterables):
            entity: Optional[JsonDict] = None
            try:
                if item is not None:
                    entity = next(item)
            except StopIteration:
                iterables[index] = None
                depleted = True
            else:
                if entity is not None:
                    yield entity
        if depleted:
            depleted = False
            iterables = [item for item in iterables if item is not None]


# pylint: disable=too-many-locals
def main():
    """Main ETL function"""
    env = dotenv_values("urbiotica.env")
    api_endpoint = "http://api.urbiotica.net/v2"
    api_organism = env['ORGANISM']
    api_username = env['USERNAME']
    api_password = env['PASSWORD']
    keystone_url = env["KEYSTONE_URL"]
    orion_url = env["ORION_URL"]
    orion_service = env["ORION_SERVICE"]
    orion_subservice = env["ORION_SUBSERVICE"]
    orion_username = env["ORION_USERNAME"]
    orion_password = env["ORION_PASSWORD"]

    session = Session()
    logging.info("Authenticating to url %s, service %s, username %s",
                 keystone_url, orion_service, orion_username)
    orion_cb = ContextBroker(keystoneURL=keystone_url,
                             orionURL=orion_url,
                             service=orion_service,
                             subservice=orion_subservice)
    orion_cb.auth(session, orion_username, orion_password)

    api = Api.login(session, api_endpoint, api_organism, api_username,
                    api_password)
    all_zones = dict()
    poms_by_zone = defaultdict(list)
    pom_params = list()
    now_ts = datetime.now()
    for project in api.projects(session).values():
        zones = project.zones(session)
        all_zones.update(zones)
        devices = dict()
        for zoneid in zones.keys():
            devices.update(project.devices(session, zoneid))
        for pom in project.spots(session).values():
            poms_by_zone[devices[pom['elementid']]['zoneid']].append(pom)
            pom_params.append({
                'session': session,
                'project': project,
                'orion_cb': orion_cb,
                'pom': pom,
                'device': devices[pom['elementid']],
                'to_ts': now_ts,
            })

    with ThreadPoolExecutor(max_workers=8) as pool:
        iterators = pool.map(lambda p: SpotIterator.collect(**p), pom_params)

    # Use rotate to improve batching (batches cannot include the same entity twice)
    orion_cb.batch(session, rotate(iterators))

    #entities = list()
    #for zoneid, zone in all_zones.items():
    #    zone_poms = poms_by_zone[zoneid]
    #    entities.append(zone_to_entity(zone, zone_poms))
    #orion_cb.batch(session, entities)


if __name__ == "__main__":

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    root.addHandler(handler)

    try:
        main()
        print("ETL OK")
    # pylint: disable=broad-except
    except Exception as err:
        print("ETL KO: ", err)
        traceback.print_exc()

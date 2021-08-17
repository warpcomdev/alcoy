# Urbiotica ETL

This ETL collects information from Urbiotica API to update ParkingSpot entities in Thinking Cities platform.

## Configuration

The ETL uses the following configuration variables, that can be passwd in either as command line flags, environment variables, or inside an `.ini` config file:

 - `-c CONFIG`, `--config CONFIG` (env `CONFIG_FILE`): config file path
- `--api-url` (env `API_URL`): Urbiotica API URL
- `--api-organism` (env `API_ORGANISM`): Organism ID for urbiotica API
- `--api-username` (env `API_USERNAME`): Username for urbiotica API
- `--api-password` (env `API_PASSWORD`): Password for urbiotica API
- `--keystone-url` (env `KEYSTONE_URL`): Keystone URL
- `--orion-url` (env `ORION_URL`): Orion URL
- `--orion-service` (env `ORION_SERVICE`): Orion service name
- `--orion-subservice` (env `ORION_SUBSERVICE`): Orion subservice name
- `--orion-username` (env `ORION_USERNAME`): Orion username
- `--orion-password` (env `ORION_PASSWORD`): Orion password

Example of `.ini` config file in [urbiotica.ini.sample](urbiotica.ini.sample)

## Behaviour

The ETL performs the following tasks:

- Connects and authenticates to both Urbiotica API and Keystone API.
- Discovers all the available projects, parkings, zones and spots for the given organization in the Urbiotica API.
- Maps each spot `pomid` to a ParkingSpot `entityID`, by prefixing with the string `pomid:`. E.g. pomid `45890` becomes EntityID `pomid:45890`. 
- For each `ParkingSpot`, queries the Orion API for the latest `occupancyModified` date.
- For each `spot`, queries the urbiotica API for `vehicle_ctrl` phenomenons since the latest `occupancyModified` date (if there is not a matching `ParkingSpot` for the spot, or the `occupancyModified` attribute is empty, defaults to last 24 hours).
- For each phenomenon, updates the corresponding `ParkingSpot` entity.

The ETL batches updates to different `ParkingSpot`s, to make it more efficient.

## Attributes

This is the mapping between Urbiotica's spot and phenomenon attributes, and `ParkingSpot` entity attributes:

- the `pomid` of the spot is mapped to the Entity's ID, by prefixing with the `pomid:` string.  E.g. pomid `45890` becomes EntityID `pomid:45890`.
- The `name` of the spot is mapped to the Entity's `name` attribute.
- The `latitude` and `longitude` of the spot are mapped to the Entity's `location` attribute.
- The `elementid` of the spot is mapped to the Entity's `refDevice` attribute, by prefixing it with the `elementid:` string. E.g. elementid `00112233445566` becomes refDevice `elementid:00112233445566`.
- The `zoneid` of the spot's elementid is mapped to the Entity's `refOnStreetParking` attribute, by prefixing it with the `zoneid:` string. E.g. zoneid `733` becomes refOnStreetParking `zoneid:733`.

  - It is assumed that each Zone will become a `OnStreetParking` entity in the model. This ETL can scan the zones, and includes a function `zone_to_entity` that can create a `OnStreetParking` entity from an Urbiotica `zone`. But this function was just used for an initial load and is not currently used for periodic updates of zones. 

- The `lstamp` of the phenomenon is mapped to both the `TimeInstant` and the `occupancyModified` attributes of the `ParkingSpot`.
- The `value` of the phenomenon is mapped to the `status` and `occupied` attributes of the `ParkingSpot`:

  - `value: '0'` is mapped to `occupied: 0`, `status: 'free'`
  - `value: '1'` is mapped to `occupied: 1`, `status: 'occupied'`
  - `value: '-1'` is mapped to `occupied: -1`, `status: 'unknown'`

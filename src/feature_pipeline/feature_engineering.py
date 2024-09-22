import json
import numpy as np
import pandas as pd
from pathlib import Path

from tqdm import tqdm
from loguru import logger
from warnings import simplefilter
from geopy.geocoders import Nominatim, Photon

from src.setup.config import config
                      

class Geocoder:
    """
    The code that makes up what is now this class was created in order to geocode an older version
    of this dataset which did not contain the coordinates of each station. In 2024's data, coordinates are
    provided, so we will not be geocoding. However, if Divvy fails to provide coordinates in future datasets,
    this code may be of use.
    """
    def __init__(self, data: pd.DataFrame, scenario: str) -> None:
        self.data = data
        self.scenario = scenario
        self.latitudes = []
        self.longitudes = []
        self.place_names = self.data[f"{scenario}_station_name"].unique().to_list()

    def geocode(self) -> dict:
        """
        Initialises the Nominatim geocoder, and applies it to a list of place names.
        It then generates the coordinates of each place and creates key, value pairs
        of each place name with its respective coordinate.

        Some of the locations will not be successfully processed by the geocoder,
        causing it (the geocoder) to return None. For each such case, the function
        provides (0,0) as the value corresponding to the place name. After this,
        the locations that were unable to be geocoded by Nominatim will be run
        through the Photon geocoder. This will (hopefully) result in the geocoding
        many of these locations. Those that are unsuccessfully geocoded will again
        have (0,0) as their corresponding coordinates.


        Returns:
            dict: a dictionary which contains key value pairs of place names and
                  coordinates
        """

        def _trigger_geocoder(geocoder: Nominatim | Photon, place_names: list) -> dict:
            """

            Args:
                geocoder: the geocoder to be used.
                place_names: the names of the places that are to be geocoded.

            Returns:
                dict: 
            """
            places_and_points = {}
            for place in place_names:
                if place in places_and_points.keys():  # The same geocoding request will not be made twice
                    continue
                else:
                    try:
                        places_and_points[f"{place}"] = geocoder.geocode(place, timeout=120)[-1]
                    except geocoder.geocode(place, timeout=120) is None:
                        logger.error(f"Failed to geocode {place}")
                        places_and_points[f"{place}"] = (0, 0)

            return places_and_points

        nominatim_results = _trigger_geocoder(geocoder=Nominatim(user_agent=config.email), place_names=self.place_names)
        places_missed_by_nominatim = [key for key, value in nominatim_results if value == (0, 0)]
        final_places_and_points = _trigger_geocoder(geocoder=Photon(), place_names=places_missed_by_nominatim)
        return final_places_and_points


class ReverseGeocoder:
    """
    This class exists to solve the problem of stations that came with no names or IDs despite having coordinates.
    It allows us to reverse geocode these coordinates in order to provide new names for these locations, and then
    produce IDs for them. This data can then be incorporated into any existing geodata.
    """
    def __init__(self, scenario: str, coordinates: list[tuple[float, float]]) -> None:
        """
        Args:
            scenario (str): "start" or "end"
            coordinates (list[tuple[float, float]]): a list of coordinates, each of which is itself a list of floating 
                                                     points.
        Returns:
            None
        """
        self.scenario = scenario
        self.coordinates = coordinates

    def reverse_geocode(self) -> list[dict[str, list[float] | str]]:
        """
        Perform reverse geocoding of each coordinate in the dataframe (avoiding duplicates), and make and return a
        dictionary of coordinates and their station addresses.

        Returns:
            list[dict[str, str | list[float]]: a list of pairings of coordinates and obtained addresses.
        """
        encountered_coordinates = []
        new_geodata: list[dict[str, str | list[float]]] = []
        primary_geocoder = Nominatim(user_agent=config.email)
        secondary_geocoder = Photon(user_agent=config.email)

        for coordinate in tqdm(iterable=self.coordinates, desc="Reverse geocoding the coordinates"):
            if coordinate not in encountered_coordinates:
                try:
                    obtained_address = str(primary_geocoder.reverse(query=coordinate, timeout=120))
                except Exception as error:
                    logger.error(error)
                    logger.warning("Reverse geocoding using Nominatim failed. Trying again with Photon...")
                    try:
                        obtained_address = str(secondary_geocoder.reverse(query=coordinate, timeout=120))
                    except Exception as error:
                        logger.error(error)
                        logger.warning("Could not reverse geocode with Photon either. This coordinate will be ignored")
                        continue

                encountered_coordinates.append(coordinate)
                new_geodata.append(
                    {
                        "station_name": obtained_address,
                        "coordinate": coordinate
                    }
                )

        return new_geodata

    @staticmethod
    def give_ids_to_the_new_names(
        new_addresses_and_coordinates: list[dict[str, list[float] | str]],
        saved_geodata:  list[dict[str, list[float] | str]]
    ):
        """

        Args:
            new_addresses_and_coordinates (list[dict[str, list[float]  |  str]]): _description_
            saved_geodata (list[dict[str, list[float]  |  str]]): _description_

        Returns:
            _type_: _description_
        """
        established_ids = []
        for station_information in tqdm(iterable=saved_geodata, desc="Finding established IDs"):
            station_id = station_information["station_id"]
            if station_id not in established_ids:
                established_ids.append(station_id)

        new_ids = np.arange(
            start=max(established_ids) + 1,
            stop=len(established_ids) + len(new_addresses_and_coordinates) + 1
        )

        new_id_index = 0
        for new_information in tqdm(
            iterable=new_addresses_and_coordinates, 
            desc="Making IDs for the newly identified stations"
        ):
            new_information["station_id"] = new_ids[new_id_index]
            new_id_index += 1

        return new_addresses_and_coordinates
        
    def put_new_information_in_geodata(
        self, 
        saved_geodata: list[dict[str, list[float] | str]], 
        new_addresses_and_coordinates: list[dict]
    ) -> list[dict]:
  
        new_addresses_and_coordinates = self.give_ids_to_the_new_names(
            saved_geodata=saved_geodata,
            new_addresses_and_coordinates=new_addresses_and_coordinates
        )

        return saved_geodata.extend(new_addresses_and_coordinates)


def add_avg_trips_last_4_weeks(features: pd.DataFrame) -> pd.DataFrame:
    """
    Include a column for the average number of trips in the past 4 weeks.

    Returns:
        pd.DataFrame: the modified dataframe
    """
    if "average_trips_last_4_weeks" not in features.columns:
        simplefilter(action="ignore", category=pd.errors.PerformanceWarning)
        averages = 0.25 * (
                features[f"trips_previous_{1 * 7 * 24}_hour"] +
                features[f"trips_previous_{2 * 7 * 24}_hour"] +
                features[f"trips_previous_{3 * 7 * 24}_hour"] +
                features[f"trips_previous_{4 * 7 * 24}_hour"]
        )

        features.insert(
            loc=features.shape[1],
            column="average_trips_last_4_weeks",
            value=averages
        )

    return features


def add_hours_and_days(features: pd.DataFrame, scenario: str) -> pd.DataFrame:
    """
    Create features which consist of the hours and days of the week on which the
    departure or arrival is taking place.

    Returns:
        pd.DataFrame: the data frame with these features included
    """

    # The values in this column change types when uploading to Hopsworks, so this avoids errors during
    # feature engineering.
    features[f"{scenario}_hour"] = pd.to_datetime(features[f"{scenario}_hour"], errors="coerce")

    times_and_entries = {
        "hour": features[f"{scenario}_hour"].apply(lambda x: x.hour),
        "day_of_the_week": features[f"{scenario}_hour"].apply(lambda x: x.dayofweek)
    }

    for time in times_and_entries.keys():
        features.insert(
            loc=features.shape[1],
            column=time,
            value=times_and_entries[time]
        )

    return features.drop(f"{scenario}_hour", axis=1)


def add_coordinates_to_dataframe(features: pd.DataFrame, scenario: str) -> pd.DataFrame:
    """
    After forming the dictionary of places and coordinates, this function isolates
    the latitudes, and longitudes and places them in appropriately named columns of
    a target dataframe.
    """
    geodata = Geocoder(data=features, scenario=scenario)
    places_and_points = geodata.geocode()

    for place in geodata.place_names:
        if place in places_and_points.keys():
            geodata.latitudes.append(places_and_points[place][0])
            geodata.longitudes.append(places_and_points[place][0])

    features[f"{scenario}_latitude"] = pd.Series(geodata.latitudes)
    features[f"{scenario}_longitude"] = pd.Series(geodata.longitudes)

    return features


def finish_feature_engineering(features: pd.DataFrame, scenario: str, geocode: bool) -> pd.DataFrame:
    """
    Initiate a chain of events that results in the accomplishment of the above feature
    engineering steps.

    Args:
        features: the features of our dataset
        scenario: whether we are looking at the starts or the ends of trips (enter "start" or "end")
        geocode: whether we want to initiate the geocoding procedures.

    Returns:
        pd.DataFrame: a dataframe containing the pre-existing features as well as the new ones.
    """
    logger.warning(f"Initiating feature engineering for the {config.displayed_scenario_names[scenario].lower()}")
    features_with_hours_and_days = add_hours_and_days(features=features, scenario=scenario)
    final_features = add_avg_trips_last_4_weeks(features=features_with_hours_and_days)

    assert "day_of_the_week" and "average_trips_last_4_weeks" in final_features.columns
    assert final_features["day_of_the_week"].isna().sum() == 0 and \
           final_features["average_trips_last_4_weeks"].isna().sum() == 0

    if geocode:
        final_features = add_coordinates_to_dataframe(features=features, scenario=scenario)
    return final_features

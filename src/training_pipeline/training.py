import pickle
import pandas as pd

from pathlib import Path
from loguru import logger
from comet_ml import Experiment
from argparse import ArgumentParser
from sklearn.metrics import mean_absolute_error
from sklearn.pipeline import Pipeline, make_pipeline

from src.setup.config import config
from src.setup.paths import MODELS_DIR, TRAINING_DATA, make_fundamental_paths
from src.feature_pipeline.preprocessing import DataProcessor
from src.training_pipeline.models import BaseModel, get_model, load_local_model
from src.training_pipeline.hyperparameter_tuning import optimise_hyperparameters

from src.inference_pipeline.model_registry_api import ModelRegistry


class Trainer:
    def __init__(
            self,
            scenario: str,
            tune_hyperparameters: bool,
            hyperparameter_trials: int
    ):
        """
        Args:
            scenario (str): a string indicating whether we are training data on the starts or ends of trips.
                            The only accepted answers are "start" and "end"

            tune_hyperparameters (bool | None, optional): whether to tune hyperparameters or not.

            hyperparameter_trials (int | None): the number of times that we will try to optimize the hyperparameters
        """
        self.scenario = scenario
        self.tune_hyperparameters = tune_hyperparameters
        self.hyperparameter_trials = hyperparameter_trials
        self.tuned_or_not = "Tuned" if self.tune_hyperparameters else "Untuned"
        make_fundamental_paths()  # Ensure that all the relevant directories exist.

    def get_or_make_training_data(self) -> tuple[pd.DataFrame, pd.Series]:
        """
        Fetches or builds the training data for the starts or ends of trips.

        Returns:
            pd.DataFrame: a tuple containing the training data's features and targets
        """
        assert self.scenario.lower() == "start" or self.scenario.lower() == "end"
        data_path = TRAINING_DATA / f"{self.scenario}s.parquet"
        if Path(data_path).is_file():
            training_data = pd.read_parquet(path=data_path)
            logger.success("The training data has already been created and saved. Fetched it...")
        else:
            logger.warning("No training data is stored. Creating the dataset will take a long time...")
            training_sets = DataProcessor(year=config.year).make_training_data(for_feature_store=False, geocode=False)
            training_data = training_sets[0] if self.scenario.lower() == "start" else training_sets[1]
            logger.success("Training data produced successfully")

        target = training_data["trips_next_hour"]
        features = training_data.drop("trips_next_hour", axis=1)
        return features.sort_index(), target.sort_index()

    def train(self, model_name: str) -> float:
        """
        The function first checks for the existence of the training data, and builds it if
        it doesn't find it locally. Then it checks for a saved model. If it doesn't find a model,
        it will go on to build one, tune its hyperparameters, save the resulting model.

        Args:
            model_name (str): the name of the model to be trained

        Returns:
            float: the error of the chosen model on the test dataset.
        """
        model_fn = get_model(model_name=model_name)
        features, target = self.get_or_make_training_data()

        train_sample_size = int(0.9 * len(features))
        x_train, x_test = features[:train_sample_size], features[train_sample_size:]
        y_train, y_test = target[:train_sample_size], target[train_sample_size:]

        experiment = Experiment(
            api_key=config.comet_api_key,
            workspace=config.comet_workspace,
            project_name=config.comet_project_name
        )
        experiment.add_tags(tags=[model_name, self.scenario])

        if isinstance(model_fn, BaseModel):
            self.tune_hyperparameters = False
        #    self.hyperparameter_trials = None

        if not self.tune_hyperparameters:
            experiment.set_name(name=f"{model_name.title()}(not tuned) model for the {self.scenario}s of trips")
            logger.info("Using the default hyperparameters")
            if model_name == "base":
                pipeline = make_pipeline(model_fn(scenario=self.scenario))
            else:
                pipeline = make_pipeline(model_fn())

        else:
            experiment.set_name(name=f"{model_name.title()}(Tuned) model for the {self.scenario}s of trips")
            logger.info(f"Tuning hyperparameters of the {model_name} model. Have a snack...")

            best_model_hyperparameters = optimise_hyperparameters(
                model_fn=model_fn,
                hyperparameter_trials=self.hyperparameter_trials,
                experiment=experiment,
                x=x_train,
                y=y_train
            )

            logger.success(f"Best model hyperparameters {best_model_hyperparameters}")
            pipeline = make_pipeline(
                model_fn(**best_model_hyperparameters)
            )

        logger.info("Fitting model...")
        # The setup base model requires that we specify these parameters, whereas with one of the other models,
        # specifying the arguments causes an error.
        pipeline.fit(X=x_train, y=y_train)
        y_pred = pipeline.predict(x_test)
        test_error = mean_absolute_error(y_true=y_test, y_pred=y_pred)

        self.save_model_locally(model_fn=pipeline, model_name=model_name)
        experiment.log_metric(name="Test M.A.E", value=test_error)
        experiment.end()
        return test_error

    def save_model_locally(self, model_fn: Pipeline, model_name: str):
        model_file_name = f"{model_name.title()} ({self.tuned_or_not} for {self.scenario}s).pkl"
        with open(MODELS_DIR/model_file_name, mode="wb") as file:
            pickle.dump(obj=model_fn, file=file)
        logger.success("Saved model to disk")

    def train_models_and_register_best(
            self,
            model_names: list[str],
            version: str,
            status: str
    ) -> None:
        """
        Train the named models, identify the best performer (on the test data) and
        return

        Args:
            model_names: the names of the models under consideration
            version:
            status:  the registered status of the m
        Returns:
            None
        """
        assert status in ["staging", "production"], 'The status must be either "staging" or "production"'
        models_and_errors = {}
        for model_name in model_names:
            test_error = self.train(model_name=model_name)
            models_and_errors[model_name] = test_error

        test_errors = models_and_errors.values()
        for model_name in model_names:
            if models_and_errors[model_name] == min(test_errors):
                logger.info(f"The best performing model is {model_name} -> Pushing it to the CometML model registry")
                model = load_local_model(model_name=model_name, scenario=self.scenario, tuned_or_not=self.tuned_or_not)

                api = ModelRegistry(
                    model=model,
                    model_name=model_name,
                    scenario=self.scenario,
                    tuned_or_not=self.tuned_or_not
                )
                api.push_model_to_registry(status=status.title(), version=version)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--scenario", type=str)
    parser.add_argument("--models", type=str, nargs="+", required=True)
    parser.add_argument("--tune_hyperparameters", action="store_true")
    parser.add_argument("--hyperparameter_trials", type=int, default=15)
    args = parser.parse_args()

    trainer = Trainer(
        scenario=args.scenario,
        tune_hyperparameters=args.tune_hyperparameters,
        hyperparameter_trials=args.hyperparameter_trials
    )

    trainer.train_models_and_register_best(model_names=args.models, version="1.0.0", status="production")

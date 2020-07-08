import math
from datetime import datetime, timedelta
import numpy as np
import logging
import pandas as pd
import os, sys, glob
from matplotlib import pyplot as plt
import us
import structlog

# from pyseir.utils import AggregationLevel, TimeseriesType
# from pyseir.utils import get_run_artifact_path, RunArtifact
from structlog.threadlocal import bind_threadlocal, clear_threadlocal, merge_threadlocal
from structlog import configure
from enum import Enum

from tensorflow import keras
from sklearn import preprocessing
from keras.models import Sequential
from keras.layers import *
from keras.callbacks import EarlyStopping

configure(processors=[merge_threadlocal, structlog.processors.KeyValueRenderer()])
log = structlog.get_logger(__name__)


class ForecastRt:
    """
    Write doc string
    """

    def __init__(self, df_all):
        log.info("init!")
        self.df_all = df_all
        self.states = "All"  # All to use All
        self.csv_path = "/Users/natashawoods/Desktop/later.nosync/covid_act_now.nosync/covid-data-model/july7_csv/"
        self.ref_date = datetime(year=2020, month=1, day=1)

        # Variable Names
        self.sim_date_name = "sim_day"
        self.predict_variable = "Rt_MAP__new_cases"
        self.d_predict_variable = f"d_{self.predict_variable}"
        self.forecast_variables = [
            "sim_day",
            "raw_new_cases",
            # "raw_new_deaths",
            self.d_predict_variable,
            "Rt_MAP__new_cases",
            "fips",
        ]
        self.scaled_variable_suffix = "_scaled"

        # Seq2Seq Parameters
        self.days_between_samples = 7
        self.mask_value = -10
        self.min_number_of_days = 30
        self.sequence_length = 100
        self.sample_train_length = 30  # Set to -1 to use all historical data
        self.predict_days = 7
        self.train_size = 0.8
        self.n_batch = 1
        self.n_epochs = 1
        self.n_hidden_layer_dimensions = 100
        self.dropout = 0
        self.patience = 50
        self.validation_split = 0.1
        log.info("DONE INIT")

    @classmethod
    def run_forecast(cls, df_all):
        try:
            log.info("CREATE CLASS")
            engine = cls(df_all)
            log.info("created class")
            return engine.forecast_rt()
        except Exception:
            logging.exception("forecast failed : ( something unintended occured")
            return None

    def get_forecast_dfs(self):
        # Probably get rid of this entirely
        if self.states != "All":
            df_all = self.df_all
            state_name = df_all["state"][0]
            df_all[self.sim_date_name] = (df_all.index - self.ref_date).days + 1
            df_all[self.d_predict_variable] = df_all[self.predict_variable].diff()
            df_forecast = df_all[self.forecast_variables].copy()
            # Fill empty values with mask value
            df_forecast.replace(r"\s+", self.mask_value, regex=True).replace("", self.mask_value)
            df_forecast.replace(np.nan, self.mask_value, regex=True).replace(
                np.nan, self.mask_value
            )
            df_forecast.to_csv(f"df_{state_name}_forecast.csv")  # , na_rep="NaN")
            return df_forecast, state_name

        else:
            dataframes = list()
            state_names = list()
            csv_files = glob.glob(f"{self.csv_path}*.csv")
            for myfile in csv_files:
                df_all = pd.read_csv(
                    myfile, parse_dates=True, index_col="date"
                )  # , dtype={'fips': int}
                state_name = df_all["state"][0]
                # Set first day of year to 1 not 0
                df_all[self.sim_date_name] = (df_all.index - self.ref_date).days + 1
                df_all[self.d_predict_variable] = df_all[self.predict_variable].diff()
                df_forecast = df_all[self.forecast_variables].copy()
                # Fill empty values with mask value
                df_forecast.replace(r"\s+", self.mask_value, regex=True).replace(
                    "", self.mask_value
                )
                df_forecast.replace(np.nan, self.mask_value, regex=True).replace(
                    np.nan, self.mask_value
                )
                df_forecast.to_csv(f"df_{state_name}_forecast.csv")  # , na_rep="NaN")
                dataframes.append(df_forecast)
                state_names.append(state_name)

            return dataframes, state_names

    def get_train_test_samples(self, df_forecast):
        df_samples = self.create_df_list(df_forecast)
        train_set_length = int(len(df_samples) * self.train_size)
        train_scaling_set = df_samples[train_set_length]
        train_samples_not_spaced = df_samples[:train_set_length]
        train_samples = train_samples_not_spaced[0 :: self.days_between_samples]
        test_samples = df_samples[train_set_length + 1 :]
        return train_samples, test_samples, train_scaling_set

    def forecast_rt(self):
        """
        predict r_t for 14 days into the future
        
        Parameters
        ___________
        df_all: dataframe with dates, new_cases, new_deaths, and r_t values

        Potential todo: add more features #ALWAYS

        Returns
        __________
        dates and forecast r_t values

        """
        log.info("saving dfall")
        log.info(self.df_all)
        # df_forecast, state_name = self.get_forecast_dfs()
        df_list, state_names = self.get_forecast_dfs()
        log.info(df_list)
        log.info("that is the df_forecasts")

        # get train, test, and scaling samples
        scaling_samples = []
        train_samples = []
        test_samples = []
        for df in df_list:
            train, test, scaling = self.get_train_test_samples(df)
            scaling_samples.append(scaling)
            train_samples.append(train)
            test_samples.append(test)

        # Get scaling dictionary
        train_scaling_set = pd.concat(scaling_samples)
        scalers_dict = self.get_scaling_dictionary(train_scaling_set)
        log.info("retrieved scaling dictionary")

        # Create scaled train samples
        list_train_X = []
        list_train_Y = []
        list_test_X = []
        list_test_Y = []
        for train, test in zip(train_samples, test_samples):
            log.info("get scaled train sets")
            train_X, train_Y, train_df_list = self.get_scaled_X_Y(train, scalers_dict)
            log.info("getting scaled test sets")
            test_X, test_Y, test_df_list = self.get_scaled_X_Y(test, scalers_dict)
            log.info("appending samples to aggregate lists")
            list_train_X.append(train_X)
            list_train_Y.append(train_Y)
            list_test_X.append(test_X)
            list_test_Y.append(test_Y)

        log.info("building model")
        model, history = self.build_model(train_X, train_Y)

        logging.info("built model")

        # Plot predictions for test and train sets

        for train_X, train_Y, test_X, test_Y, df_forecast, state_name in zip(
            list_train_X, list_train_Y, list_test_X, list_test_Y, df_list, state_names
        ):
            log.info("TRAIN FORECASTS")
            forecasts_train, dates_train = self.get_forecasts(
                train_X, train_Y, train_df_list, scalers_dict, model
            )
            log.info("TEST FORECASTS")
            forecasts_test, dates_test = self.get_forecasts(
                test_X, test_Y, test_df_list, scalers_dict, model
            )

            logging.info("about to plot")
            DATA_LINEWIDTH = 1
            MODEL_LINEWIDTH = 2
            # plot training predictions
            plt.figure(figsize=(18, 12))
            for n in range(len(dates_train)):
                i = dates_train[n]
                newdates = dates_train[n]
                # newdates = convert_to_2020_date(i,args)
                j = np.squeeze(forecasts_train[n])
                log.info("dates")
                log.info(newdates)
                log.info(j)
                if n == 0:
                    plt.plot(
                        newdates,
                        j,
                        color="green",
                        label="Train Set",
                        linewidth=MODEL_LINEWIDTH,
                        markersize=0,
                    )
                else:
                    plt.plot(newdates, j, color="green", linewidth=MODEL_LINEWIDTH, markersize=0)

                logging.info("plotted TRAIN")

            log.info("TEST___________")
            for n in range(len(dates_test)):
                i = dates_test[n]
                newdates = dates_test[n]
                # newdates = convert_to_2020_date(i,args)
                j = np.squeeze(forecasts_test[n])

                logging.info(j)
                logging.info(newdates)
                logging.info("got inputs for plotting")
                if n == 0:
                    plt.plot(
                        newdates,
                        j,
                        color="orange",
                        label="Test Set",
                        linewidth=MODEL_LINEWIDTH,
                        markersize=0,
                    )
                else:
                    plt.plot(newdates, j, color="orange", linewidth=MODEL_LINEWIDTH, markersize=0)
                logging.info("plotted TEST")

            plt.plot(
                df_forecast[self.sim_date_name],
                df_forecast[self.predict_variable],
                linewidth=DATA_LINEWIDTH,
                markersize=3,
                label="Data",
                marker="o",
            )
            plt.xlabel(self.sim_date_name)
            plt.ylabel(self.predict_variable)
            plt.legend()
            plt.grid(which="both", alpha=0.5)
            # Seq2Seq Parameters
            seq_params_dict = {
                "days_between_samples": self.days_between_samples,
                "min_number_days": self.min_number_of_days,
                "sequence_length": self.sequence_length,
                "train_length": self.sample_train_length,
                "% train": self.train_size,
                "batch size": self.n_batch,
                "epochs": self.n_epochs,
                "hidden layer dimensions": self.n_hidden_layer_dimensions,
                "dropout": self.dropout,
                "patience": self.patience,
                "validation split": self.validation_split,
                "mask value": self.mask_value,
            }
            for i, (k, v) in enumerate(seq_params_dict.items()):

                fontweight = "bold" if k in ("important variables") else "normal"

                if np.isscalar(v) and not isinstance(v, str):
                    plt.text(
                        1.0,
                        0.7 - 0.032 * i,
                        f"{k}={v:1.1f}",
                        transform=plt.gca().transAxes,
                        fontsize=15,
                        alpha=0.6,
                        fontweight=fontweight,
                    )

                else:
                    plt.text(
                        1.0,
                        0.7 - 0.032 * i,
                        f"{k}={v}",
                        transform=plt.gca().transAxes,
                        fontsize=15,
                        alpha=0.6,
                        fontweight=fontweight,
                    )

            # plt.text(4,1,t,ha='left', 'days between samples: ')
            log.info("DF FORECAST")
            log.info(df_forecast)
            plt.title(state_name + ": epochs: " + str(self.n_epochs))
            plt.savefig(
                "train_plot_" + state_name + "_epochs_" + str(self.n_epochs) + ".pdf",
                bbox_inches="tight",
            )

        return

    def get_forecasts(self, X, Y, df_list, scalers_dict, model):
        forecasts = list()
        dates = list()
        for i, j, k in zip(X, Y, df_list):
            i = i.reshape(self.n_batch, i.shape[0], i.shape[1])
            scaled_df = pd.DataFrame(np.squeeze(i))
            thisforecast = scalers_dict[self.predict_variable].inverse_transform(
                model.predict(i, batch_size=self.n_batch)
            )
            forecasts.append(thisforecast)

            last_train_day = np.array(scaled_df.iloc[-1][0]).reshape(1, -1)
            log.info(f"last train day: {last_train_day}")

            unscaled_last_train_day = scalers_dict[self.sim_date_name].inverse_transform(
                last_train_day
            )

            unscaled_first_test_day = unscaled_last_train_day + 1
            unscaled_last_test_day = int(unscaled_first_test_day) + self.predict_days - 1
            # TODO not putting int here creates weird issues that are possibly worth later investigation

            log.info(
                f"unscaled_last_train_day: {unscaled_last_train_day} first test day: {unscaled_first_test_day} last_predict_day: {unscaled_last_test_day}"
            )

            predicted_days = np.arange(unscaled_first_test_day, unscaled_last_test_day + 1.0)
            log.info("predict days")
            log.info(predicted_days)
            dates.append(predicted_days)
        return forecasts, dates

    def get_scaling_dictionary(self, train_scaling_set):
        log.info("getting scaling dictionary")
        scalers_dict = {}
        log.info("training scaling set")
        log.info(train_scaling_set)
        log.info(train_scaling_set.dtypes)
        train_scaling_set.to_csv("scalingset_now.csv")
        for columnName, columnData in train_scaling_set.iteritems():
            log.info("column")
            log.info(columnName)
            scaler = preprocessing.MinMaxScaler(feature_range=(-1, 1))
            log.info("made scaler")
            reshaped_data = columnData.values.reshape(-1, 1)
            log.info("reshaped data")

            scaler = scaler.fit(reshaped_data)
            log.info("fit data")
            scaled_values = scaler.transform(reshaped_data)
            log.info("scaled data")

            scalers_dict.update({columnName: scaler})
            log.info("saved scaler to dictionary")
        log.info("about to return scaling dictionary")
        return scalers_dict

    def get_scaled_X_Y(self, samples, scalers_dict):
        sample_list = list()
        for sample in samples:
            for columnName, columnData in sample.iteritems():
                scaled_values = scalers_dict[columnName].transform(columnData.values.reshape(-1, 1))
                sample.loc[:, f"{columnName}{self.scaled_variable_suffix}"] = scaled_values
            sample_list.append(sample)

        X, Y, df_list = self.get_X_Y(sample_list)
        return X, Y, df_list

    def build_model(self, final_train_X, final_train_Y):
        model = Sequential()
        model.add(
            Masking(
                mask_value=self.mask_value,
                batch_input_shape=(self.n_batch, final_train_X.shape[1], final_train_X.shape[2]),
            )
        )
        model.add(
            LSTM(
                self.n_hidden_layer_dimensions,
                batch_input_shape=(self.n_batch, final_train_X.shape[1], final_train_X.shape[2]),
                stateful=True,
                return_sequences=True,
            )
        )
        model.add(
            LSTM(
                self.n_hidden_layer_dimensions,
                batch_input_shape=(self.n_batch, final_train_X.shape[1], final_train_X.shape[2]),
                stateful=True,
            )
        )
        model.add(Dropout(self.dropout))
        model.add(Dense(final_train_Y.shape[1]))
        es = EarlyStopping(monitor="val_loss", mode="min", verbose=1, patience=self.patience)
        model.compile(loss="mean_squared_error", optimizer="adam")
        history = model.fit(
            final_train_X,
            final_train_Y,
            epochs=self.n_epochs,
            batch_size=self.n_batch,
            verbose=1,
            shuffle=False,
            validation_split=self.validation_split,
            callbacks=[es],
        )
        logging.info("fit")
        logging.info(history.history["loss"])
        logging.info(history.history["val_loss"])
        plot = True
        if plot:
            plt.close("all")
            logging.info("plotting")
            plt.plot(history.history["loss"], color="blue", linestyle="solid", label="Train Set")
            logging.info("plotted history")
            plt.plot(
                history.history["val_loss"],
                color="green",
                linestyle="solid",
                label="Validation Set",
            )
            logging.info("plotted more")
            plt.legend()
            plt.xlabel("Epochs")
            plt.ylabel("RMSE")
            plt.savefig("lstm_loss_final.png")
            plt.close("all")

        return model, history

    def get_X_Y(self, sample_list):
        PREDICT_VAR = self.predict_variable + self.scaled_variable_suffix
        X_train_list = list()
        Y_train_list = list()
        df_list = list()
        log.info("SAMPLE LIST LENGTH")
        log.info(len(sample_list))
        for i in range(len(sample_list)):
            df = sample_list[i]
            df_list.append(df)
            df = df.filter(regex="scaled")

            train = df.iloc[
                : -self.predict_days, :
            ]  # exclude last n entries of df to use for prediction
            test = df.iloc[-self.predict_days :, :]

            n_rows_train = train.shape[0]
            n_rows_to_add = self.sequence_length - n_rows_train
            pad_rows = np.empty((n_rows_to_add, train.shape[1]), float)
            pad_rows[:] = self.mask_value
            padded_train = np.concatenate((pad_rows, train))

            test = np.array(test[PREDICT_VAR])

            X_train_list.append(padded_train)
            Y_train_list.append(test)

        final_test_X = np.array(X_train_list)
        final_test_Y = np.array(Y_train_list)
        final_test_Y = np.squeeze(final_test_Y)
        return final_test_X, final_test_Y, df_list

    def create_df_list(self, df):
        df_list = list()
        for index in range(len(df.index) + 1):
            i = index
            # i = index * self.days_between_samples
            # i = index
            # if i > len(df.index):
            #    continue

            if (
                i < self.predict_days + self.min_number_of_days
            ):  # only keep df if it has min number of entries
                continue
            else:
                if self.sample_train_length == -1:  # use all historical data for every sample
                    df_list.append(df[:i].copy())
                else:  # use only SAMPLE_LENGTH historical days of data
                    df_list.append(df[i - self.sample_train_length : i].copy())
        return df_list

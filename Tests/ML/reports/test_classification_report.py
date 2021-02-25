#  ------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
#  ------------------------------------------------------------------------------------------
import math
import shutil
from pathlib import Path
from io import StringIO

import numpy as np
import pandas as pd
import pytest

from InnerEye.Common.metrics_constants import LoggingColumns
from InnerEye.Common.output_directories import OutputFolderForTests
from InnerEye.ML.reports.classification_report import ReportedMetrics, get_correct_and_misclassified_examples, \
    get_image_filepath_from_subject_id, get_k_best_and_worst_performing, get_metric, get_labels_and_predictions, \
    plot_image_from_filepath, get_image_labels_from_subject_id, get_image_outputs_from_subject_id
from InnerEye.ML.reports.notebook_report import generate_classification_notebook
from InnerEye.ML.scalar_config import ScalarModelBase
from InnerEye.ML.configs.classification.DummyMulticlassClassification import DummyMulticlassClassification
from InnerEye.ML.metrics_dict import MetricsDict
from InnerEye.Azure.azure_util import DEFAULT_CROSS_VALIDATION_SPLIT_INDEX


def test_generate_classification_report(test_output_dirs: OutputFolderForTests) -> None:
    reports_folder = Path(__file__).parent
    test_metrics_file = reports_folder / "test_metrics_classification.csv"
    val_metrics_file = reports_folder / "val_metrics_classification.csv"
    dataset_csv_path = reports_folder / 'dataset.csv'

    result_file = test_output_dirs.root_dir / "report.ipynb"
    config = ScalarModelBase()
    config.label_value_column = "label"
    config.image_file_column = "filePath"
    result_html = generate_classification_notebook(result_notebook=result_file,
                                                   config=config,
                                                   val_metrics=val_metrics_file,
                                                   test_metrics=test_metrics_file,
                                                   dataset_csv_path=dataset_csv_path)
    assert result_file.is_file()
    assert result_html.is_file()
    assert result_html.suffix == ".html"


def test_get_labels_and_predictions() -> None:
    reports_folder = Path(__file__).parent
    test_metrics_file = reports_folder / "test_metrics_classification.csv"
    results = get_labels_and_predictions(test_metrics_file, MetricsDict.DEFAULT_HUE_KEY)
    assert all([results.subject_ids[i] == i for i in range(12)])
    assert all([results.labels[i] == label for i, label in enumerate([1] * 6 + [0] * 6)])
    assert all([results.model_outputs[i] == op for i, op in enumerate([0.0, 0.2, 0.4, 0.6, 0.8, 1.0] * 2)])


def test_functions_with_invalid_csv(test_output_dirs: OutputFolderForTests) -> None:
    reports_folder = Path(__file__).parent
    test_metrics_file = reports_folder / "test_metrics_classification.csv"
    val_metrics_file = reports_folder / "val_metrics_classification.csv"
    invalid_metrics_file = Path(test_output_dirs.root_dir) / "invalid_metrics_classification.csv"
    shutil.copyfile(test_metrics_file, invalid_metrics_file)
    # Duplicate a subject
    with open(invalid_metrics_file, "a") as file:
        file.write(f"{MetricsDict.DEFAULT_HUE_KEY},1,5,1.0,1,-1,Test")
    with pytest.raises(ValueError) as ex:
        get_labels_and_predictions(invalid_metrics_file, MetricsDict.DEFAULT_HUE_KEY)
    assert "Subject IDs should be unique" in str(ex)

    with pytest.raises(ValueError) as ex:
        get_correct_and_misclassified_examples(invalid_metrics_file, test_metrics_file, MetricsDict.DEFAULT_HUE_KEY)
    assert "Subject IDs should be unique" in str(ex)

    with pytest.raises(ValueError) as ex:
        get_correct_and_misclassified_examples(val_metrics_file, invalid_metrics_file, MetricsDict.DEFAULT_HUE_KEY)
    assert "Subject IDs should be unique" in str(ex)


def test_get_metric() -> None:
    reports_folder = Path(__file__).parent
    test_metrics_file = reports_folder / "test_metrics_classification.csv"
    val_metrics_file = reports_folder / "val_metrics_classification.csv"

    val_metrics = get_labels_and_predictions(val_metrics_file, MetricsDict.DEFAULT_HUE_KEY)
    test_metrics = get_labels_and_predictions(test_metrics_file, MetricsDict.DEFAULT_HUE_KEY)

    optimal_threshold = get_metric(test_metrics=test_metrics,
                                   val_metrics=val_metrics,
                                   metric=ReportedMetrics.OptimalThreshold)

    assert optimal_threshold == 0.6

    optimal_threshold = get_metric(test_metrics=test_metrics,
                                   val_metrics=val_metrics,
                                   metric=ReportedMetrics.OptimalThreshold,
                                   optimal_threshold=0.3)

    assert optimal_threshold == 0.3

    auc_roc = get_metric(test_metrics=test_metrics,
                         val_metrics=val_metrics,
                         metric=ReportedMetrics.AUC_ROC)
    assert auc_roc == 0.5

    auc_pr = get_metric(test_metrics=test_metrics,
                        val_metrics=val_metrics,
                        metric=ReportedMetrics.AUC_PR)

    assert math.isclose(auc_pr, 13 / 24, abs_tol=1e-15)

    accuracy = get_metric(test_metrics=test_metrics,
                          val_metrics=val_metrics,
                          metric=ReportedMetrics.Accuracy)

    assert accuracy == 0.5

    accuracy = get_metric(test_metrics=test_metrics,
                          val_metrics=val_metrics,
                          metric=ReportedMetrics.Accuracy,
                          optimal_threshold=0.1)

    assert accuracy == 0.5

    fpr = get_metric(test_metrics=test_metrics,
                     val_metrics=val_metrics,
                     metric=ReportedMetrics.FalsePositiveRate)

    assert fpr == 0.5

    fpr = get_metric(test_metrics=test_metrics,
                     val_metrics=val_metrics,
                     metric=ReportedMetrics.FalsePositiveRate,
                     optimal_threshold=0.1)

    assert fpr == 5 / 6

    fnr = get_metric(test_metrics=test_metrics,
                     val_metrics=val_metrics,
                     metric=ReportedMetrics.FalseNegativeRate)

    assert fnr == 0.5

    fnr = get_metric(test_metrics=test_metrics,
                     val_metrics=val_metrics,
                     metric=ReportedMetrics.FalseNegativeRate,
                     optimal_threshold=0.1)

    assert math.isclose(fnr, 1 / 6, abs_tol=1e-15)


def test_get_correct_and_misclassified_examples() -> None:
    reports_folder = Path(__file__).parent
    test_metrics_file = reports_folder / "test_metrics_classification.csv"
    val_metrics_file = reports_folder / "val_metrics_classification.csv"

    results = get_correct_and_misclassified_examples(val_metrics_csv=val_metrics_file,
                                                     test_metrics_csv=test_metrics_file)

    true_positives = [item[LoggingColumns.Patient.value] for _, item in results.true_positives.iterrows()]
    assert all([i in true_positives for i in [3, 4, 5]])

    true_negatives = [item[LoggingColumns.Patient.value] for _, item in results.true_negatives.iterrows()]
    assert all([i in true_negatives for i in [6, 7, 8]])

    false_positives = [item[LoggingColumns.Patient.value] for _, item in results.false_positives.iterrows()]
    assert all([i in false_positives for i in [9, 10, 11]])

    false_negatives = [item[LoggingColumns.Patient.value] for _, item in results.false_negatives.iterrows()]
    assert all([i in false_negatives for i in [0, 1, 2]])


def test_get_k_best_and_worst_performing() -> None:
    reports_folder = Path(__file__).parent
    test_metrics_file = reports_folder / "test_metrics_classification.csv"
    val_metrics_file = reports_folder / "val_metrics_classification.csv"

    results = get_k_best_and_worst_performing(val_metrics_csv=val_metrics_file,
                                              test_metrics_csv=test_metrics_file,
                                              k=2)

    best_true_positives = [item[LoggingColumns.Patient.value] for _, item in results.true_positives.iterrows()]
    assert best_true_positives == [5, 4]

    best_true_negatives = [item[LoggingColumns.Patient.value] for _, item in results.true_negatives.iterrows()]
    assert best_true_negatives == [6, 7]

    worst_false_positives = [item[LoggingColumns.Patient.value] for _, item in results.false_positives.iterrows()]
    assert worst_false_positives == [11, 10]

    worst_false_negatives = [item[LoggingColumns.Patient.value] for _, item in results.false_negatives.iterrows()]
    assert worst_false_negatives == [0, 1]


def test_get_image_filepath_from_subject_id_single() -> None:
    reports_folder = Path(__file__).parent
    dataset_csv_file = reports_folder / "dataset.csv"
    dataset_df = pd.read_csv(dataset_csv_file, dtype=str)

    config = ScalarModelBase(image_file_column="filePath")

    filepath = get_image_filepath_from_subject_id(subject_id="1",
                                                  dataset_df=dataset_df,
                                                  config=config,
                                                  dataset_dir=reports_folder)
    expected_path = Path(reports_folder / "../test_data/classification_data_2d/im2.npy")

    assert filepath
    assert len(filepath) == 1
    assert expected_path.samefile(filepath[0])


def test_get_image_filepath_from_subject_id_with_image_channels(test_output_dirs: OutputFolderForTests) -> None:
    image_file_name = "image.npy"
    dataset_csv_file = StringIO(f"subject,channel,filePath\n"
                                f"0,channel1,{image_file_name}\n"
                                f"0,channel2,\n"
                                f"1,channel1,{image_file_name}\n"
                                f"1,channel2,\n")

    dataset_df = pd.read_csv(dataset_csv_file, dtype=str)

    config = ScalarModelBase(image_channels=["channel1"],
                             image_file_column="filePath")
    filepath = get_image_filepath_from_subject_id(subject_id="1",
                                                  dataset_df=dataset_df,
                                                  config=config,
                                                  dataset_dir=test_output_dirs.root_dir)

    assert filepath
    assert len(filepath) == 1
    assert filepath[0] == test_output_dirs.root_dir / image_file_name


def test_get_image_filepath_from_subject_id_multiple() -> None:
    reports_folder = Path(__file__).parent
    dataset_csv_file = reports_folder / "dataset.csv"
    dataset_df = pd.read_csv(dataset_csv_file, dtype=str)
    config = DummyMulticlassClassification()
    config.subject_column = "subject"
    config.image_file_column = "filePath"

    # duplicate subject entries
    dataset_df.loc[::2, config.subject_column] = [str(i) for i in list(range(len(dataset_df)//2))]
    dataset_df.loc[1::2, config.subject_column] = [str(i) for i in list(range(len(dataset_df)//2))]

    filepath = get_image_filepath_from_subject_id(subject_id="1",
                                                  dataset_df=dataset_df,
                                                  config=config,
                                                  dataset_dir=reports_folder)
    expected_paths = [Path(reports_folder / "../test_data/classification_data_2d/im1.npy"),
                      Path(reports_folder / "../test_data/classification_data_2d/im2.npy")]

    assert filepath
    assert len(filepath) == 2
    assert expected_paths[0].samefile(filepath[0])
    assert expected_paths[1].samefile(filepath[1])


def test_get_image_filepath_from_subject_id_invalid_id() -> None:
    reports_folder = Path(__file__).parent
    dataset_csv_file = reports_folder / "dataset.csv"
    dataset_df = pd.read_csv(dataset_csv_file, dtype=str)
    config = DummyMulticlassClassification()
    config.subject_column = "subject"
    config.image_file_column = "filePath"

    filepath = get_image_filepath_from_subject_id(subject_id="100",
                                                  dataset_df=dataset_df,
                                                  config=config,
                                                  dataset_dir=reports_folder)

    assert not filepath


def test_image_labels_from_subject_id_single() -> None:
    reports_folder = Path(__file__).parent
    dataset_csv_file = reports_folder / "dataset.csv"
    dataset_df = pd.read_csv(dataset_csv_file, dtype=str)

    config = ScalarModelBase(label_value_column="label")

    labels = get_image_labels_from_subject_id(subject_id="1",
                                              dataset_df=dataset_df,
                                              config=config)
    assert not labels


def test_image_labels_from_subject_id_with_label_channels() -> None:
    dataset_csv_file = StringIO("subject,channel,label\n"
                                "0,channel1,0\n"
                                "0,channel2,\n"
                                "1,channel1,1\n"
                                "1,channel2,\n")

    dataset_df = pd.read_csv(dataset_csv_file, dtype=str)

    config = ScalarModelBase(label_channels=["channel1"],
                             label_value_column="label")
    labels = get_image_labels_from_subject_id(subject_id="1",
                                              dataset_df=dataset_df,
                                              config=config)
    assert labels
    assert len(labels) == 1
    assert labels[0] == MetricsDict.DEFAULT_HUE_KEY


def test_image_labels_from_subject_id_multiple() -> None:
    reports_folder = Path(__file__).parent
    dataset_csv_file = reports_folder / "dataset.csv"
    dataset_df = pd.read_csv(dataset_csv_file, dtype=str)

    config = DummyMulticlassClassification()
    config.subject_column = "subject"

    # Add multiple labels for a single subject
    dataset_df.loc[:, config.label_value_column] = "1|2"

    labels = get_image_labels_from_subject_id(subject_id="1",
                                                  dataset_df=dataset_df,
                                                  config=config)
    assert labels
    assert len(labels) == 2
    assert set(labels) == {"class1", "class2"}


def test_plot_image_from_filepath(test_output_dirs: OutputFolderForTests) -> None:
    im_width = 200

    array = np.ones([10, 10])
    valid_file = Path(test_output_dirs.root_dir) / "valid.npy"
    np.save(valid_file, array)
    res = plot_image_from_filepath(valid_file, im_width)
    assert res

    array = np.ones([3, 10, 10])
    invalid_file = Path(test_output_dirs.root_dir) / "invalid.npy"
    np.save(invalid_file, array)
    res = plot_image_from_filepath(invalid_file, im_width)
    assert not res


def test_get_image_outputs_from_subject_id(test_output_dirs: OutputFolderForTests) -> None:
    hues = ["Hue1", "Hue2"]

    metrics_df = pd.DataFrame.from_dict({LoggingColumns.Hue.value: [hues[0], hues[1]] * 6,
                                         LoggingColumns.Epoch.value: [0] * 12,
                                         LoggingColumns.Patient.value: [s for s in range(6) for _ in range(2)],
                                         LoggingColumns.ModelOutput.value: [0.1, 0.1, 0.1, 0.9, 0.1, 0.9,
                                                                            0.9, 0.9, 0.9, 0.9, 0.9, 0.1],
                                         LoggingColumns.Label.value: [0, 0, 0, 1, 0, 1, 1, 1, 1, 1, 1, 0],
                                         LoggingColumns.CrossValidationSplitIndex: [DEFAULT_CROSS_VALIDATION_SPLIT_INDEX] * 12,
                                         LoggingColumns.DataSplit.value: [0] * 12,
                                         }, dtype=str)

    config = DummyMulticlassClassification()
    config.class_names = hues
    config.subject_column = "subject"

    model_output = get_image_outputs_from_subject_id(subject_id="1",
                                                     metrics_df=metrics_df)
    assert model_output
    assert len(model_output) == 2
    assert all([m == e for m, e in zip(model_output, [(hues[0], 0.1), (hues[1], 0.9)])])

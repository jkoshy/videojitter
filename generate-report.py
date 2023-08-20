#!/usr/bin/env python3

import altair as alt
import argparse
import numpy as np
import json
import pandas as pd
import scipy.signal
import sys


def parse_arguments():
    argument_parser = argparse.ArgumentParser(
        description="Given a spec file, and recording analysis results passed in stdin, produces a summary of the results.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    argument_parser.add_argument(
        "--spec-file",
        help="Path to the input spec file",
        required=True,
        type=argparse.FileType(),
        default=argparse.SUPPRESS,
    )
    argument_parser.add_argument(
        "--output-file",
        help="Path to the output file where the summary will be written. Format is determined from the file extension. Recommended format is HTML. Information on other formats is available at https://altair-viz.github.io/user_guide/saving_charts.html",
        required=True,
        default=argparse.SUPPRESS,
    )
    argument_parser.add_argument(
        "--compensate-clock-skew",
        help="Calculate and compensate for clock skew, i.e. the difference in speed between the recording clock and the video clock that would otherwise result in a sloped/tilted graph. Note that the clock skew estimate can be incorrect (e.g. for recordings where the overall mean error undergoes sudden changes), leading to odd results.",
        action="store_true",
        default=False,
    )
    return argument_parser.parse_args()


def interval(series):
    return pd.Interval(series.min(), series.max())


def rescale(series, target_range):
    original_range = interval(series)
    return (
        series - original_range.left
    ) / original_range.length * target_range.length + target_range.left


def generate_report():
    args = parse_arguments()

    spec = json.load(args.spec_file)
    nominal_fps = spec["fps"]["num"] / spec["fps"]["den"]
    frame_duration = spec["fps"]["den"] / spec["fps"]["num"]
    reference_transitions = pd.DataFrame(
        {"frame": spec["frames"]},
        index=pd.Index(
            np.arange(0, len(spec["frames"])) * frame_duration,
            name="timestamp_seconds",
        ),
    )
    reference_transitions_diff = (
        reference_transitions.loc[:, "frame"]
        != reference_transitions.loc[:, "frame"].shift()
    )
    reference_transitions_diff[0] = False
    reference_transitions.loc[:, "previous_frame_count"] = (
        reference_transitions.groupby(reference_transitions_diff.cumsum())
        .cumcount()
        .shift()
        + 1
    )
    reference_transitions = reference_transitions[reference_transitions_diff]
    reference_transitions_interval_seconds = interval(reference_transitions.index)
    print(
        f"Successfully loaded spec file containing {reference_transitions.size} frame transitions at {nominal_fps} FPS, with first transition at {reference_transitions_interval_seconds.left} seconds and last transition at {reference_transitions_interval_seconds.right} seconds for a total of {reference_transitions_interval_seconds.length} seconds",
        file=sys.stderr,
    )

    transitions = pd.read_csv(
        sys.stdin,
        index_col="recording_timestamp_seconds",
        usecols=["recording_timestamp_seconds", "frame"],
    )
    transitions_interval_seconds = interval(transitions.index)
    print(
        f"Recording analysis contains {transitions.index.size} frame transitions, with first transition at {transitions_interval_seconds.left} seconds and last transition at {transitions_interval_seconds.right} seconds for a total of {transitions_interval_seconds.length} seconds",
        file=sys.stderr,
    )
    if transitions.index.size == reference_transitions.index.size:
        print("Number of recorded transitions matches the spec. Good.", file=sys.stderr)
    else:
        print(
            "WARNING: number of recorded transitions is inconsistent with the spec. Either the recording is corrupted, or the video player skipped/repeated some transitions entirely.",
            file=sys.stderr,
        )

    transitions.reset_index(inplace=True)
    transitions.index = pd.Index(
        rescale(
            transitions.loc[:, "recording_timestamp_seconds"],
            interval(reference_transitions.index),
        ),
        name="scaled_recording_timestamp_seconds",
    )
    # TODO: check that the black/white frames match
    transitions = pd.merge(
        left=reference_transitions.rename(
            lambda column_name: "reference_" + column_name, axis="columns"
        ),
        left_index=True,
        right=pd.merge_asof(
            left=transitions,
            left_index=True,
            right=reference_transitions.index.to_series().rename(
                "reference_timestamp_seconds"
            ),
            right_index=True,
            direction="nearest",
        ),
        right_on="reference_timestamp_seconds",
        how="outer",
    )
    transitions.loc[:, "repeated"] = transitions.loc[
        :, "reference_timestamp_seconds"
    ].duplicated(keep=False)
    transitions.loc[:, "expected_recording_timestamp_seconds"] = rescale(
        transitions.loc[:, "reference_timestamp_seconds"],
        interval(transitions.loc[:, "recording_timestamp_seconds"]),
    )
    transitions.loc[:, "error_seconds"] = (
        transitions.loc[:, "recording_timestamp_seconds"]
        - transitions.loc[:, "reference_timestamp_seconds"]
    )
    # TODO: find a better way to calculate clock skew so that we can enable
    # clock skew compensation by default. The current method produces
    # nonsensical results in some cases; for example the slope part of the
    # linear regression breaks down if the mean suddenly changes in the middle
    # of the recording.
    valid_transitions = transitions.loc[
        ~(
            pd.isna(transitions.loc[:, "recording_timestamp_seconds"])
            | transitions.loc[:, "repeated"]
        ),
        :,
    ]
    linear_regression = np.polynomial.Polynomial.fit(
        valid_transitions.loc[:, "recording_timestamp_seconds"],
        valid_transitions.loc[:, "error_seconds"],
        deg=1 if args.compensate_clock_skew else 0,
    )
    if args.compensate_clock_skew:
        clock_skew = 1 + linear_regression.coef[1]
        if abs(linear_regression.coef[1]) > 0.10:
            print(
                f"WARNING: abnormally large clock skew detected - recording is {clock_skew}x longer than expected.",
                file=sys.stderr,
            )
        else:
            print(
                f"Recording is {clock_skew}x longer than expected. This is usually due to benign clock skew. Scaling timestamps to compensate.",
                file=sys.stderr,
            )
    transitions.loc[:, "error_seconds"] -= linear_regression(
        transitions.loc[:, "recording_timestamp_seconds"]
    )

    black_offset = transitions.loc[
        transitions.loc[:, "frame"] == "BLACK", "error_seconds"
    ].mean()
    white_offset = transitions.loc[
        transitions.loc[:, "frame"] == "WHITE", "error_seconds"
    ].mean()
    print(
        f"Offsets black: {white_offset} seconds white: {black_offset} seconds",
        file=sys.stderr,
    )
    transitions.loc[
        transitions.loc[:, "frame"] == "BLACK", "error_seconds"
    ] -= black_offset
    transitions.loc[
        transitions.loc[:, "frame"] == "WHITE", "error_seconds"
    ] -= white_offset

    print(
        f"Error standard deviation: {transitions.loc[:, 'error_seconds'].std()} seconds",
        file=sys.stderr,
    )

    chart = alt.Chart(transitions)
    chart_samples = (
        chart.transform_calculate(
            label="Transition to "
            + alt.expr.if_(alt.datum["reference_frame"], "white", "black")
            + " (after "
            + alt.datum["reference_previous_frame_count"]
            + " "
            + alt.expr.if_(alt.datum["reference_frame"], "black", "white")
            + " frames)"
        )
        .mark_point()
        .encode(
            alt.X("estimated_recording_timestamp_seconds", type="quantitative").scale(
                zero=False
            ),
            alt.Y("error_seconds").scale(zero=False),
            alt.Color("label", type="nominal", title=None),
        )
    )
    chart_anomalies = (
        chart.transform_calculate(
            anomaly=alt.expr.if_(
                alt.datum["repeated"],
                "Repeated transition",
                alt.expr.if_(
                    alt.expr.isValid(alt.datum["recording_timestamp_seconds"]),
                    None,
                    "Missing transition",
                ),
            ),
        )
        .transform_filter(alt.expr.isValid(alt.datum["anomaly"]))
        .mark_rule(strokeWidth=2)
        .encode(
            alt.X("estimated_recording_timestamp_seconds", type="quantitative"),
            alt.Color("anomaly", type="nominal", title=None).scale(
                domain=["Missing transition", "Repeated transition"],
                range=["orangered", "orange"],
            ),
        )
    )
    (chart_anomalies + chart_samples).properties(
        width=1000, height=750
    ).transform_calculate(
        estimated_recording_timestamp_seconds=alt.expr.if_(
            alt.expr.isValid(alt.datum["recording_timestamp_seconds"]),
            alt.datum["recording_timestamp_seconds"],
            alt.datum["expected_recording_timestamp_seconds"],
        )
    ).resolve_scale(
        color="independent"
    ).configure_legend(
        labelLimit=0
    ).save(
        args.output_file
    )


generate_report()

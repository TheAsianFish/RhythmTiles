"""Cluster MERT embeddings into intensity-labeled sections.

Algorithm:
  1. Downsample embeddings temporally (~50 fps -> ~1 fps) so KMeans
     operates on coarse musical sections, not noisy per-frame jitter.
  2. KMeans with K=4 (intro/verse/chorus/bridge as a rough mapping).
     We don't try to NAME them - we just want grouped frames.
  3. Smooth cluster assignments via a median filter so 1-second
     hiccups don't become micro-sections.
  4. Merge contiguous runs of the same cluster into Section records.
  5. Drop sections shorter than MIN_SECTION_S; merge them into the
     adjacent section with the longer duration.
  6. Label each section by its mean RMS energy on the original audio:
     bottom third = bucket 0, top third = bucket 2, middle = 1.

The bucket value is what chart_builder reads. Cluster id is preserved
for debugging.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

# Coarsen the embedding frame rate before clustering. MERT emits ~50
# frames/sec; sections we care about are tens of seconds long, so a
# bucket per second is plenty of resolution and keeps KMeans cheap.
TARGET_FRAMES_PER_SEC = 1.0

# Smoothing window for the cluster-assignment sequence. 5 seconds at
# 1 fps removes single-second flips without erasing real transitions.
SMOOTH_WINDOW_FRAMES = 5

# Minimum section length in seconds. Shorter than this, the section is
# probably a clustering artifact (a fill, a build, a momentary
# texture change). Merge into the longer neighbour.
MIN_SECTION_S = 6.0

# Number of clusters to look for. 4 maps loosely to
# intro/verse/chorus/bridge for typical pop. We don't try to label them
# by name - just to give the chart-builder distinct buckets to drive
# density. Larger K splits chorus sub-sections; smaller K collapses
# verse + chorus.
DEFAULT_K_CLUSTERS = 4


@dataclass
class LabeledSection:
    start_s: float
    end_s: float
    bucket: int            # 0=low, 1=mid, 2=high intensity
    cluster_id: int
    intensity: float       # mean RMS of the section, raw value


def sections_from_embeddings(
    *,
    embeddings: "np.ndarray",
    frame_rate_hz: float,
    y: "np.ndarray",
    sr: int,
    k: int = DEFAULT_K_CLUSTERS,
) -> list[LabeledSection]:
    """Cluster MERT embeddings, label sections by RMS intensity.

    Returns an empty list if the audio is too short for clustering to
    be meaningful (we need at least ~K * MIN_SECTION_S of audio).
    """
    import numpy as np  # noqa: WPS433

    if embeddings.size == 0 or frame_rate_hz <= 0:
        return []

    duration_s = embeddings.shape[0] / frame_rate_hz
    if duration_s < k * MIN_SECTION_S:
        # Not enough audio for K distinct sections; bail out so the
        # caller falls back to RMS bucketing.
        return []

    coarsened, coarse_times = _coarsen_embeddings(
        embeddings=embeddings, frame_rate_hz=frame_rate_hz,
    )
    if coarsened.shape[0] < k:
        return []

    cluster_assignments = _kmeans_cluster(coarsened, k=k)
    cluster_assignments = _smooth_assignments(cluster_assignments)
    raw_sections = _runs_to_sections(
        assignments=cluster_assignments, times=coarse_times,
    )
    merged = _merge_short_sections(raw_sections)
    labeled = _label_by_intensity(merged, y=y, sr=sr)
    return labeled


def bucket_at(t: float, sections: list[LabeledSection]) -> int:
    """Return the bucket of the section containing time t.

    Returns 1 (mid) if no section contains t (very start / very end of
    audio); 1 keeps the thinner's density multiplier neutral.
    """
    if not sections:
        return 1
    # Linear scan; section lists are small (a dozen at most).
    for s in sections:
        if s.start_s <= t < s.end_s:
            return s.bucket
    # Past the last section: use the last bucket.
    if t >= sections[-1].end_s:
        return sections[-1].bucket
    return 1


def buckets_for_note_times(
    *,
    note_times_s: list[float],
    sections: list[LabeledSection],
) -> list[int]:
    """Vectorised lookup for a list of note times. Returns buckets in order.

    Uses a sweep pointer to stay O(n + m) instead of O(n * m).
    """
    out: list[int] = []
    if not sections:
        return [1] * len(note_times_s)
    j = 0
    nsec = len(sections)
    for t in note_times_s:
        while j + 1 < nsec and sections[j + 1].start_s <= t:
            j += 1
        # Past the last section: use the last bucket.
        if t >= sections[-1].end_s:
            out.append(sections[-1].bucket)
            continue
        s = sections[j]
        if s.start_s <= t < s.end_s:
            out.append(s.bucket)
        else:
            out.append(1)
    return out


def _coarsen_embeddings(
    *,
    embeddings: "np.ndarray",
    frame_rate_hz: float,
) -> tuple["np.ndarray", "np.ndarray"]:
    """Mean-pool consecutive frames to reach ~TARGET_FRAMES_PER_SEC.

    Returns (coarse_embeddings, coarse_times) where coarse_times[i] is
    the centre time of the i-th coarse frame in seconds.
    """
    import numpy as np  # noqa: WPS433

    group = max(1, int(round(frame_rate_hz / TARGET_FRAMES_PER_SEC)))
    n_frames = embeddings.shape[0]
    n_groups = n_frames // group
    if n_groups == 0:
        return embeddings, np.zeros(n_frames, dtype=np.float32)

    trimmed = embeddings[: n_groups * group]
    reshaped = trimmed.reshape(n_groups, group, embeddings.shape[1])
    coarse = reshaped.mean(axis=1).astype(np.float32)
    # Time of each coarse frame's centre = (group index + 0.5) * group / fps.
    coarse_times = (
        (np.arange(n_groups, dtype=np.float32) + 0.5) * group / frame_rate_hz
    )
    return coarse, coarse_times


def _kmeans_cluster(x: "np.ndarray", *, k: int) -> "np.ndarray":
    """KMeans on the embeddings. Returns per-frame cluster id.

    Uses scikit-learn (already a transitive dep via librosa). Fixed
    random_state for determinism.
    """
    from sklearn.cluster import KMeans  # noqa: WPS433

    km = KMeans(n_clusters=k, random_state=0, n_init=10)
    return km.fit_predict(x)


def _smooth_assignments(assignments: "np.ndarray") -> "np.ndarray":
    """Median-filter the cluster sequence to remove 1-frame jitters."""
    import numpy as np  # noqa: WPS433

    n = len(assignments)
    if n <= SMOOTH_WINDOW_FRAMES:
        return assignments
    half = SMOOTH_WINDOW_FRAMES // 2
    out = np.empty_like(assignments)
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        window = assignments[lo:hi]
        # mode = most-common value in the window
        vals, counts = np.unique(window, return_counts=True)
        out[i] = vals[counts.argmax()]
    return out


def _runs_to_sections(
    *,
    assignments: "np.ndarray",
    times: "np.ndarray",
) -> list[LabeledSection]:
    """Convert per-frame cluster ids into LabeledSection records.

    Intensity is left at 0.0; the labeller fills it in afterwards.
    """
    if len(assignments) == 0:
        return []
    sections: list[LabeledSection] = []
    run_start = 0
    current = int(assignments[0])
    for i in range(1, len(assignments)):
        if int(assignments[i]) != current:
            sections.append(
                LabeledSection(
                    start_s=float(times[run_start]),
                    end_s=float(times[i]),
                    bucket=1,
                    cluster_id=current,
                    intensity=0.0,
                ),
            )
            run_start = i
            current = int(assignments[i])
    # Tail.
    sections.append(
        LabeledSection(
            start_s=float(times[run_start]),
            end_s=float(times[-1]),
            bucket=1,
            cluster_id=current,
            intensity=0.0,
        ),
    )
    return sections


def _merge_short_sections(
    sections: list[LabeledSection],
) -> list[LabeledSection]:
    """Drop sections shorter than MIN_SECTION_S by merging into a neighbour.

    Strategy: walk left-to-right. If a section is too short, merge it
    into the previous section (extending the previous section's end).
    The first section is merged forward if it's too short. Keeps cluster
    id of the absorbing section.
    """
    if not sections:
        return sections
    out: list[LabeledSection] = []
    for s in sections:
        duration = s.end_s - s.start_s
        if duration < MIN_SECTION_S and out:
            # Merge into previous: extend the previous section's end.
            prev = out[-1]
            out[-1] = LabeledSection(
                start_s=prev.start_s,
                end_s=s.end_s,
                bucket=prev.bucket,
                cluster_id=prev.cluster_id,
                intensity=prev.intensity,
            )
            continue
        if duration < MIN_SECTION_S and not out:
            # First section too short: keep but it'll get extended by
            # the next iteration. Treat it as a normal section since
            # there's no previous to merge into.
            out.append(s)
            continue
        out.append(s)
    return out


def _label_by_intensity(
    sections: list[LabeledSection],
    *,
    y: "np.ndarray",
    sr: int,
) -> list[LabeledSection]:
    """Assign each section a 0/1/2 bucket based on its mean RMS.

    Uses 33/67 percentile thresholds across the per-section RMS values
    so even an "all chorus" song gets some sections in each bucket.
    """
    import librosa  # noqa: WPS433
    import numpy as np  # noqa: WPS433

    if not sections:
        return sections
    rms = librosa.feature.rms(y=y, hop_length=512)[0]
    fps = sr / 512.0

    intensities: list[float] = []
    for s in sections:
        lo = max(0, int(s.start_s * fps))
        hi = min(len(rms), int(s.end_s * fps))
        if hi <= lo:
            intensities.append(0.0)
            continue
        intensities.append(float(rms[lo:hi].mean()))

    if len(intensities) < 3:
        # Too few sections to bucket meaningfully; everyone mid.
        return [
            LabeledSection(
                start_s=s.start_s,
                end_s=s.end_s,
                bucket=1,
                cluster_id=s.cluster_id,
                intensity=intensities[i],
            )
            for i, s in enumerate(sections)
        ]

    q33 = float(np.quantile(intensities, 0.33))
    q67 = float(np.quantile(intensities, 0.67))
    out: list[LabeledSection] = []
    for s, energy in zip(sections, intensities):
        if energy <= q33:
            bucket = 0
        elif energy >= q67:
            bucket = 2
        else:
            bucket = 1
        out.append(
            LabeledSection(
                start_s=s.start_s,
                end_s=s.end_s,
                bucket=bucket,
                cluster_id=s.cluster_id,
                intensity=energy,
            ),
        )
    return out

def union_ranges(ranges):
    """Validate and merge half-open integer intervals, including adjacency."""
    if not isinstance(ranges, list):
        raise ValueError("ranges must be a list")

    intervals = []
    for interval in ranges:
        if not isinstance(interval, (list, tuple)) or len(interval) != 2:
            raise ValueError("each interval must contain exactly two endpoints")
        start, end = interval
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
        ):
            raise ValueError("endpoints must be integers, excluding bool")
        if start > end:
            raise ValueError("start must not exceed end")
        if start < end:
            intervals.append([start, end])

    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged

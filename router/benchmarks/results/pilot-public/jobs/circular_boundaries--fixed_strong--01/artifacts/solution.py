def owner(period, segments, t):
    """Return the highest-ranked segment covering t on a circular period."""
    if not isinstance(period, int) or isinstance(period, bool) or period <= 0:
        raise ValueError("period must be a positive integer")
    if not isinstance(t, int) or isinstance(t, bool):
        raise ValueError("t must be an integer")

    if isinstance(segments, (str, bytes, bytearray, dict)):
        raise ValueError("segments must be an iterable of segment dictionaries")
    try:
        segment_iterator = iter(segments)
    except TypeError:
        raise ValueError("segments must be an iterable of segment dictionaries") from None

    point = t % period
    required_keys = {"id", "start", "end", "priority"}
    seen_ids = set()
    best_rank = None
    best_id = None

    for segment in segment_iterator:
        if not isinstance(segment, dict) or set(segment) != required_keys:
            raise ValueError("each segment must contain exactly id, start, end, priority")

        segment_id = segment["id"]
        start = segment["start"]
        end = segment["end"]
        priority = segment["priority"]

        if not isinstance(segment_id, str) or not segment_id:
            raise ValueError("segment id must be a nonempty string")
        if segment_id in seen_ids:
            raise ValueError("segment ids must be unique")
        seen_ids.add(segment_id)

        if not isinstance(start, int) or isinstance(start, bool):
            raise ValueError("start must be an integer")
        if not isinstance(end, int) or isinstance(end, bool):
            raise ValueError("end must be an integer")
        if not 0 <= start < period or not 0 <= end < period:
            raise ValueError("start and end must be within the period")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ValueError("priority must be an integer")

        length = (end - start) % period
        if length == 0:
            length = period

        if (point - start) % period < length:
            rank = (-priority, length, segment_id)
            if best_rank is None or rank < best_rank:
                best_rank = rank
                best_id = segment_id

    return best_id

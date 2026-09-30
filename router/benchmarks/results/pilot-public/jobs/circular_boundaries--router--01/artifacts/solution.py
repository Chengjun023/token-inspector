def owner(period, segments, t):
    """Return the winning segment at t on a circular period."""
    if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        raise ValueError("period must be a positive integer")
    if isinstance(t, bool) or not isinstance(t, int):
        raise ValueError("t must be an integer")
    if isinstance(segments, (str, bytes, dict)):
        raise ValueError("segments must be an iterable of segment dictionaries")
    try:
        iterator = iter(segments)
    except TypeError:
        raise ValueError("segments must be an iterable of segment dictionaries") from None

    position = t % period
    seen = set()
    best_key = None
    best_id = None
    for segment in iterator:
        if not isinstance(segment, dict) or set(segment) != {"id", "start", "end", "priority"}:
            raise ValueError("each segment must have exactly id, start, end, and priority")
        segment_id = segment["id"]
        start = segment["start"]
        end = segment["end"]
        priority = segment["priority"]
        if not isinstance(segment_id, str) or not segment_id or segment_id in seen:
            raise ValueError("segment ids must be unique nonempty strings")
        for endpoint in (start, end):
            if isinstance(endpoint, bool) or not isinstance(endpoint, int) or not 0 <= endpoint < period:
                raise ValueError("endpoints must be integers within the period")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("priority must be an integer")
        seen.add(segment_id)

        length = (end - start) % period
        if length == 0:
            length = period
        if (position - start) % period < length:
            key = (-priority, length, segment_id)
            if best_key is None or key < best_key:
                best_key = key
                best_id = segment_id
    return best_id

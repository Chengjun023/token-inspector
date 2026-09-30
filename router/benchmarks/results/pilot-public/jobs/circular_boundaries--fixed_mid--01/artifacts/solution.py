def owner(period, segments, t):
    """Return the winning segment id at t on a circular period."""
    if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        raise ValueError("period must be a positive integer")
    if isinstance(t, bool) or not isinstance(t, int):
        raise ValueError("t must be an integer")
    if isinstance(segments, (str, bytes, bytearray, dict)):
        raise ValueError("segments must be an iterable of segment dictionaries")
    try:
        segment_iterator = iter(segments)
    except TypeError:
        raise ValueError("segments must be iterable") from None

    position = t % period
    seen_ids = set()
    best_key = None
    winner = None

    for segment in segment_iterator:
        if not isinstance(segment, dict) or set(segment) != {"id", "start", "end", "priority"}:
            raise ValueError("each segment must have exactly id, start, end, and priority")

        segment_id = segment["id"]
        start = segment["start"]
        end = segment["end"]
        priority = segment["priority"]

        if not isinstance(segment_id, str) or not segment_id:
            raise ValueError("segment id must be a nonempty string")
        if segment_id in seen_ids:
            raise ValueError("segment ids must be unique")
        seen_ids.add(segment_id)

        for endpoint in (start, end):
            if isinstance(endpoint, bool) or not isinstance(endpoint, int):
                raise ValueError("segment endpoints must be integers")
            if not 0 <= endpoint < period:
                raise ValueError("segment endpoints must be within the period")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("segment priority must be an integer")

        if start == end:
            covers = True
            length = period
        elif start < end:
            covers = start <= position < end
            length = end - start
        else:
            covers = position >= start or position < end
            length = period - start + end

        if covers:
            key = (-priority, length, segment_id)
            if best_key is None or key < best_key:
                best_key = key
                winner = segment_id

    return winner

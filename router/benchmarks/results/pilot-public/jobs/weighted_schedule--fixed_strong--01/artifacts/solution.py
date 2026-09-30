from bisect import bisect_right


def select_jobs(jobs):
    """Return optimal compatible job IDs in chronological order.

    Uses weighted interval scheduling with O(n^2) time and space,
    including the sequences retained for tie-breaking.
    """
    if isinstance(jobs, (str, bytes, bytearray, dict)):
        raise ValueError("jobs must be an iterable of job dictionaries")
    try:
        iterator = iter(jobs)
    except TypeError:
        raise ValueError("jobs must be an iterable of job dictionaries") from None

    required_keys = {"id", "start", "end", "reward"}
    seen_ids = set()
    ordered = []
    for job in iterator:
        if not isinstance(job, dict) or set(job) != required_keys:
            raise ValueError("each job must have exactly id, start, end, reward")

        job_id = job["id"]
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job IDs must be nonempty strings")
        if job_id in seen_ids:
            raise ValueError("job IDs must be unique")

        start = job["start"]
        end = job["end"]
        reward = job["reward"]
        for value in (start, end, reward):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError("start, end, and reward must be integers")
        if start >= end:
            raise ValueError("start must be less than end")

        seen_ids.add(job_id)
        ordered.append((end, start, job_id, reward))

    ordered.sort()
    ends = [job[0] for job in ordered]
    best = [(0, ())]

    for index, (end, start, job_id, reward) in enumerate(ordered):
        compatible_count = bisect_right(ends, start, 0, index)
        prior_reward, prior_ids = best[compatible_count]
        take_reward = prior_reward + reward
        take_ids = prior_ids + (job_id,)
        skip_reward, skip_ids = best[-1]

        take_is_better = take_reward > skip_reward
        if take_reward == skip_reward:
            take_is_better = (
                len(take_ids) < len(skip_ids)
                or (
                    len(take_ids) == len(skip_ids)
                    and take_ids < skip_ids
                )
            )

        if take_is_better:
            best.append((take_reward, take_ids))
        else:
            best.append((skip_reward, skip_ids))

    return list(best[-1][1])

from bisect import bisect_left


def select_jobs(jobs):
    """Select compatible jobs by reward, count, then chronological ID sequence."""
    if isinstance(jobs, (str, bytes, dict)):
        raise ValueError("jobs must be an iterable of job dictionaries")
    try:
        iterator = iter(jobs)
    except TypeError:
        raise ValueError("jobs must be an iterable of job dictionaries") from None

    records = []
    seen = set()
    for job in iterator:
        if not isinstance(job, dict) or set(job) != {"id", "start", "end", "reward"}:
            raise ValueError("each job must contain exactly id, start, end, and reward")
        job_id = job["id"]
        if not isinstance(job_id, str) or not job_id or job_id in seen:
            raise ValueError("job IDs must be unique nonempty strings")
        start, end, reward = job["start"], job["end"], job["reward"]
        for value in (start, end, reward):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError("start, end, and reward must be integers")
        if start >= end:
            raise ValueError("start must be less than end")
        seen.add(job_id)
        records.append((start, end, job_id, reward))

    records.sort(key=lambda record: (record[0], record[1], record[2]))
    starts = [record[0] for record in records]
    n = len(records)
    best = [(0, ())] * (n + 1)

    for index in range(n - 1, -1, -1):
        start, end, job_id, reward = records[index]
        next_index = bisect_left(starts, end, index + 1)
        following_reward, following_ids = best[next_index]
        take_reward = reward + following_reward
        take_ids = (job_id,) + following_ids
        skip_reward, skip_ids = best[index + 1]

        if (
            take_reward > skip_reward
            or (
                take_reward == skip_reward
                and (
                    len(take_ids) < len(skip_ids)
                    or (
                        len(take_ids) == len(skip_ids)
                        and take_ids < skip_ids
                    )
                )
            )
        ):
            best[index] = (take_reward, take_ids)
        else:
            best[index] = (skip_reward, skip_ids)

    return list(best[0][1])

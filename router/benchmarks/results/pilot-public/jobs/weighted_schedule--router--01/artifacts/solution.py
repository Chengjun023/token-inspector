from bisect import bisect_right


def select_jobs(jobs):
    """Return the optimal compatible job IDs in chronological order."""
    if isinstance(jobs, (str, bytes, dict)):
        raise ValueError("jobs must be an iterable of job dictionaries")
    try:
        items = list(jobs)
    except TypeError:
        raise ValueError("jobs must be an iterable of job dictionaries") from None

    seen = set()
    validated = []
    for job in items:
        if not isinstance(job, dict) or set(job) != {"id", "start", "end", "reward"}:
            raise ValueError("each job must have exactly id, start, end, and reward")
        job_id = job["id"]
        if not isinstance(job_id, str) or not job_id or job_id in seen:
            raise ValueError("job IDs must be unique nonempty strings")
        start, end, reward = job["start"], job["end"], job["reward"]
        for number in (start, end, reward):
            if not isinstance(number, int) or isinstance(number, bool):
                raise ValueError("job numbers must be integers, excluding bool")
        if start >= end:
            raise ValueError("job start must be less than end")
        seen.add(job_id)
        validated.append((start, end, job_id, reward))

    validated.sort(key=lambda job: (job[1], job[0], job[2]))
    ends = [job[1] for job in validated]
    best = [(0, ())]
    for index, (start, end, job_id, reward) in enumerate(validated):
        compatible_count = bisect_right(ends, start, 0, index)
        previous_reward, previous_ids = best[compatible_count]
        candidate = (previous_reward + reward, previous_ids + (job_id,))
        incumbent = best[-1]
        if (
            candidate[0] > incumbent[0]
            or (
                candidate[0] == incumbent[0]
                and (
                    len(candidate[1]) < len(incumbent[1])
                    or (
                        len(candidate[1]) == len(incumbent[1])
                        and candidate[1] < incumbent[1]
                    )
                )
            )
        ):
            best.append(candidate)
        else:
            best.append(incumbent)
    return list(best[-1][1])

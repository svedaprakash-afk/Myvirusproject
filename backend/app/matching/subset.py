"""Bounded subset-sum: which combination of open invoices explains a payment? (DFS with pruning)"""
from __future__ import annotations


def find_subsets(values: list[tuple[int, int]], target: int, tol: int, max_size: int = 8, max_results: int = 6,
                 node_limit: int = 150_000) -> list[tuple[list[int], int]]:
    """values: [(key, amount>0)], returns up to max_results [(keys, sum)] with |sum - target| <= tol.
    Results sorted by (|diff|, size). Larger-first ordering + suffix-sum pruning keeps this fast in practice;
    node_limit is a hard stop so pathological inputs can never stall the matching run."""
    items = sorted(((v, k) for k, v in values if v > 0), reverse=True)
    n = len(items)
    suffix = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = suffix[i + 1] + items[i][0]
    results: list[tuple[list[int], int]] = []
    nodes = 0
    chosen: list[int] = []

    def dfs(i: int, total: int) -> None:
        nonlocal nodes
        if nodes > node_limit or len(results) >= max_results * 4:
            return
        nodes += 1
        if chosen and abs(total - target) <= tol:
            results.append(([items[j][1] for j in chosen], total))
        if i >= n or len(chosen) >= max_size:
            return
        for j in range(i, n):
            v = items[j][0]
            if total + v > target + tol:
                continue  # too big (items sorted desc, later ones may fit)
            if total + suffix[j] < target - tol:
                break  # even taking everything left can't reach the target
            chosen.append(j)
            dfs(j + 1, total + v)
            chosen.pop()

    dfs(0, 0)
    results.sort(key=lambda r: (abs(r[1] - target), len(r[0])))
    return results[:max_results]

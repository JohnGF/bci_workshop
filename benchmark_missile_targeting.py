import timeit
import random

def generate_entities(num_missiles, num_enemies):
    missiles = [{"x": random.uniform(0, 800), "y": random.uniform(0, 600)} for _ in range(num_missiles)]
    enemies = [{"x": random.uniform(0, 800), "y": random.uniform(0, 600)} for _ in range(num_enemies)]
    return missiles, enemies

def original_method(missiles, enemies):
    for m in missiles:
        if enemies:
            nearest = min(enemies, key=lambda e: (e["x"] - m["x"])**2 + (e["y"] - m["y"])**2)
            dx = nearest["x"] - m["x"]
            dy = nearest["y"] - m["y"]

def optimized_method_1(missiles, enemies):
    for m in missiles:
        if not enemies:
            continue

        mx, my = m["x"], m["y"]

        nearest = enemies[0]
        min_dist_sq = (nearest["x"] - mx)**2 + (nearest["y"] - my)**2

        for i in range(1, len(enemies)):
            e = enemies[i]
            dist_sq = (e["x"] - mx)**2 + (e["y"] - my)**2
            if dist_sq < min_dist_sq:
                min_dist_sq = dist_sq
                nearest = e

        dx = nearest["x"] - mx
        dy = nearest["y"] - my

def optimized_method_3(missiles, enemies):
    for m in missiles:
        if not enemies:
            continue
        mx, my = m["x"], m["y"]

        nearest = enemies[0]
        min_dist_sq = (nearest["x"] - mx)**2 + (nearest["y"] - my)**2

        for e in enemies[1:]:
            dist_sq = (e["x"] - mx)**2 + (e["y"] - my)**2
            if dist_sq < min_dist_sq:
                min_dist_sq = dist_sq
                nearest = e

        dx = nearest["x"] - mx
        dy = nearest["y"] - my

def run_benchmarks():
    for counts in [(10, 10), (100, 100), (500, 500)]:
        num_missiles, num_enemies = counts
        missiles, enemies = generate_entities(num_missiles, num_enemies)

        print(f"--- Benchmark with {num_missiles} missiles and {num_enemies} enemies ---")

        orig_time = timeit.timeit(lambda: original_method(missiles, enemies), number=100)
        print(f"Original Method:  {orig_time:.5f} seconds")

        opt_time1 = timeit.timeit(lambda: optimized_method_1(missiles, enemies), number=100)
        print(f"Method 1 (loop with indexing): {opt_time1:.5f} seconds")
        print(f"  Speedup: {orig_time / opt_time1:.2f}x faster")

        opt_time3 = timeit.timeit(lambda: optimized_method_3(missiles, enemies), number=100)
        print(f"Method 3 (loop over slice): {opt_time3:.5f} seconds")
        print(f"  Speedup: {orig_time / opt_time3:.2f}x faster")

        print()

if __name__ == "__main__":
    run_benchmarks()

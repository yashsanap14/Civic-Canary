"""One-time indexing of existing S3 run records for bounded Recent Runs retrieval."""

from services.monitoring import index_run
from services.store_factory import default_store


def main():
    store = default_store()
    count = 0
    for run in store.list_runs():
        index_run(store, run)
        count += 1
    print(f"Indexed {count} existing runs")


if __name__ == "__main__":
    main()

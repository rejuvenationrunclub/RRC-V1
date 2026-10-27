"""Application services: transactions, locking, idempotency and outbox writes.

Every function takes an ``AsyncSession`` already inside a transaction; the caller
commits. Member-level writes lock the member row first (plan §1.4), and unique
constraints make retries return the earlier result instead of doing work twice.
"""

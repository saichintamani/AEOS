"""
Distributed contracts package — interfaces only, zero implementations.

All ABCs in this package define the boundaries that implementations
(Kafka, Redis, gRPC, in-memory) must satisfy. Code in higher layers
depends on these contracts, never on concrete implementations.

Contract: AC-DEP-005 (no circular deps), AC-IFACE-001 (required interfaces)
"""

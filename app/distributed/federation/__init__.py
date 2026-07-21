"""AEOS federation gateway package — cross-cluster trust control plane.

The runnable entrypoint is ``python -m app.distributed.federation`` (see
``__main__``). It composes the pre-existing federation gRPC servicer, the local
scheduler admission path, and the JWKS provider into a single deployable
process; it introduces no new federation capability.
"""

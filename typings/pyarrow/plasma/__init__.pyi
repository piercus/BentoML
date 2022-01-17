import typing as t


class ObjectID:
    def __init__(self, _b: bytes) -> None:
        ...

    def binary(self) -> bytes:
        ...

class PlasmaClient:
    def put(self, data: t.Any) -> ObjectID:
        ...

    def get(self, object_id: ObjectID) -> t.Any:
        ...


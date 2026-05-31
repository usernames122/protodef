# protodef

Tiny JSON `.protodef` protobuf wire encoder/decoder.

This is not a protobuf compiler. It only stores enough information to encode and decode protobuf wire data from Python dictionaries.

```python
from protodef import load_schema

schema = load_schema("Media.protodef")

payload = schema.encode("Media.UploadedFileRef", {
    "id": 123,
    "name": "cat.png",
    "partCount": 1,
})

print(payload.hex())
print(schema.decode("Media.UploadedFileRef", payload))
```

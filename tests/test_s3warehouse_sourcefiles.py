from datetime import datetime, timedelta

import pytest
from inveniautils.stream import SeekableStream

from datawarehouse import exceptions
from datawarehouse.interface import DataWarehouseInterface as API
from tests.aws_setup import get_warehouse_sesh, mock_start, mock_stop, setup_resources
from tests.utils import get_streams, register_test_collections


@pytest.fixture()
def warehouse():
    mock_start()
    setup_resources()
    wh = get_warehouse_sesh()
    register_test_collections(wh)
    yield wh
    mock_stop()


def _load_file_versions(warehouse):
    """ Helper method to load in a few file versions. """
    # pkey: "url"  -  type_map: {url: STR, filename: STR}
    warehouse.select_collection("test_collection", database="test_database")
    # This loads in 4 SeekableStreams with the same Primary Key but unique content.
    files = get_streams("test_database", "test_collection")
    files.sort(key=lambda f: f.metadata["retrieved_date"])
    file_key = warehouse.get_primary_key(files[0].metadata)
    assert all(warehouse.get_primary_key(f.metadata) == file_key for f in files)
    assert len(set(f.read() for f in files)) == len(files)
    assert len(files) == 4
    _ = [f.seek(0) for f in files]  # rewind all files
    return file_key, files


def test_get_primary_key(warehouse):
    # single val pkey: key fields: ("key13", )
    warehouse.select_collection("load_forecast", database="ercot")
    metadata = {"key13": "some-key", "key0": 1234}
    assert warehouse.get_primary_key(metadata) == ("some-key",)

    # 2-val pkey, key fields: ("key1", "key2")
    warehouse.select_collection("realtime_price", database="caiso")
    metadata = {"key1": datetime(2020, 1, 1), "filename": "file2.txt"}
    with pytest.raises(exceptions.MetadataError):
        warehouse.get_primary_key(metadata)  # missing key field: "key2"

    metadata["key2"] = "123456"
    with pytest.raises(exceptions.MetadataError):
        warehouse.get_primary_key(metadata)  # pkey fields "key2" has invalid type

    metadata["key2"] = 123456
    assert warehouse.get_primary_key(metadata) == (datetime(2020, 1, 1), 123456)


def test_get_source_version(warehouse):
    metadata = {"key1": "some-key", "key0": 1234}
    # missing version fields from metadata
    with pytest.raises(exceptions.MetadataError):
        warehouse.get_source_version(metadata)
    # version field must be a string, not int.
    metadata[API.VERSION_FIELD] = 12345678
    with pytest.raises(exceptions.MetadataError):
        warehouse.get_source_version(metadata)
    # test pass
    metadata[API.VERSION_FIELD] = "12345678"
    assert warehouse.get_source_version(metadata) == "12345678"


def test_bucket_prefix(warehouse):
    file_key, files = _load_file_versions(warehouse)

    pfx = "my_prefix"
    client = get_warehouse_sesh(db="test_database", coll="test_collection", prefix=pfx)
    client.store(files[0])

    stored = client.retrieve(file_key, metadata_only=True)
    assert stored[client.S3_KEY_FIELD].startswith(pfx)


def test_string_and_byte_files(warehouse):
    file_key, files = _load_file_versions(warehouse)
    content = str(files[0].read())
    metadata = files[0].metadata

    string_file = SeekableStream(content, **metadata)
    response = warehouse.store(string_file, force_store=True)
    version = response[API.VERSION_FIELD]
    stored = warehouse.retrieve(file_key, version)
    assert stored.is_bytes is False

    byte_file = SeekableStream(content.encode(), **metadata)
    response = warehouse.store(byte_file, force_store=True)
    version = response[API.VERSION_FIELD]
    stored = warehouse.retrieve(file_key, version)
    assert stored.is_bytes is True


def test_store_and_force_store_duplicate_versions(warehouse):
    # -pkey: "url" -rkey: "last-modified" -type_map: {url: STR, last-modified: DATETIME}
    warehouse.select_collection("test_last_modified", database="test_database")
    # load in a test file.
    file = get_streams("test_database", "test_last_modified")[0]
    content = file.read()
    file.seek(0)

    # Store the test file.
    response = warehouse.store(file)
    assert response["primary_key"] == warehouse.get_primary_key(file.metadata)
    assert response["status_code"] == warehouse.STATUS.SUCCESS
    version_1 = response[API.VERSION_FIELD]

    # An identical new file, duplicate is not stored.
    newfile = SeekableStream(content, **file.metadata)
    response = warehouse.store(newfile)
    assert response["status_code"] == warehouse.STATUS.ALREADY_EXIST
    assert response[API.VERSION_FIELD] == version_1

    # Updated last-modified but still identical content, duplicate is not stored.
    newfile = SeekableStream(content, **file.metadata)
    newfile.metadata[API.LAST_MODIFIED_FIELD] += timedelta(days=1)
    response = warehouse.store(newfile)
    assert response["status_code"] == warehouse.STATUS.ALREADY_EXIST
    assert response[API.VERSION_FIELD] == version_1

    # Updated content but last-modified not changed, still not stored.
    # This is because the collection has set the "last-modified" field as required.
    newfile = SeekableStream(content + "some new content", **file.metadata)
    response = warehouse.store(newfile)
    assert response["status_code"] == warehouse.STATUS.ALREADY_EXIST
    assert response[API.VERSION_FIELD] == version_1

    # Identical content and last-modified, but use force store.
    newfile = SeekableStream(content, **file.metadata)
    response = warehouse.store(newfile, force_store=True)
    assert response["status_code"] == warehouse.STATUS.SUCCESS
    assert response[API.VERSION_FIELD] != version_1
    version_2 = response[API.VERSION_FIELD]

    # Updated content and new last-modified, this is stored.
    newfile = SeekableStream(content + "some new content", **file.metadata)
    newfile.metadata[API.LAST_MODIFIED_FIELD] += timedelta(days=1)
    response = warehouse.store(newfile)
    assert response["status_code"] == warehouse.STATUS.SUCCESS
    assert response[API.VERSION_FIELD] != version_1
    assert response[API.VERSION_FIELD] != version_2

    # check how many versions are stored in total, it should only be 3
    file_key = warehouse.get_primary_key(file.metadata)
    stored_versions = warehouse.retrieve_versions(file_key)
    assert len(list(stored_versions)) == 3


def test_store_and_retrieve_specify_version(warehouse):
    # returns multiple versions of a file (loaded from a test file, not in warehouse)
    file_key, files = _load_file_versions(warehouse)
    source_versions = []

    # Store all versions in the warehouse.
    for file in files:
        response = warehouse.store(file)
        assert response["primary_key"] == file_key
        assert response["status_code"] == warehouse.STATUS.SUCCESS
        source_versions.append(response[API.VERSION_FIELD])
    # unique version ids
    assert len(set(source_versions)) == len(files)

    # warehouse.retrieve() grabs latest version by default.
    latest_stored = warehouse.retrieve(file_key)
    assert warehouse.get_primary_key(latest_stored.metadata) == file_key
    assert warehouse.get_source_version(latest_stored.metadata) == source_versions[-1]
    assert latest_stored.read() == files[-1].read()
    files[-1].seek(0)

    # grab specific versions
    for i, version in enumerate(source_versions):
        stored = warehouse.retrieve(file_key, version)
        assert warehouse.get_primary_key(stored.metadata) == file_key
        assert warehouse.get_source_version(stored.metadata) == version
        assert stored.read() == files[i].read()


def test_store_and_retrieve_multiple_versions(warehouse):
    # returns multiple versions of a file (loaded from a test file, not in warehouse)
    file_key, files = _load_file_versions(warehouse)

    # Store File 0 (retrieved_date: 2020-01-02)
    response = warehouse.store(files[0])
    assert response["status_code"] == warehouse.STATUS.SUCCESS

    # Store File 2 (retrieved_date: 2020-03-02)
    response = warehouse.store(files[2])
    assert response["status_code"] == warehouse.STATUS.SUCCESS
    version_2 = response[API.VERSION_FIELD]
    # warehouse.retrieve() grabs latest version (File 2) by default.
    stored = warehouse.retrieve(file_key)
    assert warehouse.get_source_version(stored.metadata) == version_2
    assert stored.read() == files[2].read()
    files[2].seek(0)

    # Store File 1 (retrieved_date: 2020-02-02, Newer than File 0, Older than File2)
    response = warehouse.store(files[1])
    assert response["status_code"] == warehouse.STATUS.SUCCESS
    version_1 = response[API.VERSION_FIELD]
    # warehouse.retrieve() grabs latest version by default, still File 2, not File 1.
    stored = warehouse.retrieve(file_key)
    assert warehouse.get_source_version(stored.metadata) != version_1
    assert warehouse.get_source_version(stored.metadata) == version_2
    stored_content = stored.read()
    assert stored_content != files[1].read()
    assert stored_content == files[2].read()
    files[1].seek(0)
    files[2].seek(0)

    # Store File 3 (retrieved_date: 2020-04-02, The newest so far.)
    response = warehouse.store(files[3])
    assert response["status_code"] == warehouse.STATUS.SUCCESS
    version_3 = response[API.VERSION_FIELD]
    # warehouse.retrieve() grabs this latest version.
    stored = warehouse.retrieve(file_key)
    assert warehouse.get_source_version(stored.metadata) == version_3
    assert stored.read() == files[3].read()
    files[3].seek(0)

    # warehouse.retrieve_versions() grabs files in retrieved_date order.
    stored_versions = warehouse.retrieve_versions(file_key, latest_first=True)
    for file in reversed(files):  # our files list are sorted by earliset first.
        stored = next(stored_versions)
        assert stored.read() == file.read()
        assert stored.metadata[API.VERSION_FIELD] == file.metadata[API.VERSION_FIELD]
        assert stored.metadata[API.RELEASE_FIELD] == file.metadata[API.RELEASE_FIELD]
        file.seek(0)

    # Flip the latest_first flag to False.
    stored_versions = warehouse.retrieve_versions(file_key, latest_first=False)
    for file in files:
        stored = next(stored_versions)
        assert stored.read() == file.read()
        assert stored.metadata[API.VERSION_FIELD] == file.metadata[API.VERSION_FIELD]
        assert stored.metadata[API.RELEASE_FIELD] == file.metadata[API.RELEASE_FIELD]
        file.seek(0)


def test_store_with_compare_func(warehouse):
    # returns multiple versions of a file (loaded from a test file, not in warehouse)
    file_key, files = _load_file_versions(warehouse)
    file = files[0]
    content = file.read()
    file.seek(0)

    # compar funcs
    always_equal = lambda file_1, file_2: True
    always_not_equal = lambda file_1, file_2: False

    # First file of a primary key is always stored, compare func is ignored.
    response = warehouse.store(file, compare_source=always_equal)
    assert response["primary_key"] == file_key
    assert response["status_code"] == warehouse.STATUS.SUCCESS
    version_1 = response[API.VERSION_FIELD]

    # An identical copy is not stored by default.
    newfile = SeekableStream(content, **file.metadata)
    response = warehouse.store(newfile)
    assert response["status_code"] == warehouse.STATUS.ALREADY_EXIST
    assert response[API.VERSION_FIELD] == version_1

    # Store the duplicate again with a compare func that always returns False.
    newfile = SeekableStream(content, **file.metadata)
    response = warehouse.store(newfile, compare_source=always_not_equal)
    assert response["status_code"] == warehouse.STATUS.SUCCESS
    assert response[API.VERSION_FIELD] != version_1

    # Store a non-duplicate with a compare func that always returns True.
    newfile = SeekableStream(content + "some more content", **file.metadata)
    response = warehouse.store(newfile, compare_source=always_equal)
    assert response["status_code"] == warehouse.STATUS.ALREADY_EXIST

    # Try again without a compare func, this time it is stored.
    newfile = SeekableStream(content + "some more content", **file.metadata)
    response = warehouse.store(newfile)
    assert response["status_code"] == warehouse.STATUS.SUCCESS


def test_store_missing_fields(warehouse):
    # returns multiple versions of a file (loaded from a test file, not in warehouse)
    file_key, files = _load_file_versions(warehouse)
    content = files[0].read()

    required = list(warehouse.required_metadata_fields)  # ("url", "filename")
    required.extend([API.RETRIEVED_FIELD, API.RELEASE_FIELD])

    # storing the file will fail if any one of the required fields are missing
    for field in required:
        file = SeekableStream(content, **files[0].metadata)
        file.metadata.pop(field)
        with pytest.raises(exceptions.MetadataError):
            warehouse.store(file)


def test_retrieve_metadata_only(warehouse):
    # returns multiple versions of a file (loaded from a test file, not in warehouse)
    file_key, files = _load_file_versions(warehouse)
    source_versions = []

    # Store all versions in the warehouse.
    for file in files:
        response = warehouse.store(file)
        assert response["primary_key"] == file_key
        assert response["status_code"] == warehouse.STATUS.SUCCESS
        source_versions.append(response[API.VERSION_FIELD])
    # unique version ids
    assert len(set(source_versions)) == len(files)

    # warehouse.retrieve() grabs latest version by default.
    latest_stored = warehouse.retrieve(file_key, metadata_only=True)
    assert isinstance(latest_stored, dict)
    assert warehouse.get_primary_key(latest_stored) == file_key
    assert warehouse.get_source_version(latest_stored) == source_versions[-1]

    # grab specific versions
    for i, version in enumerate(source_versions):
        stored = warehouse.retrieve(file_key, version, metadata_only=True)
        assert isinstance(stored, dict)
        assert warehouse.get_primary_key(stored) == file_key
        assert warehouse.get_source_version(stored) == version

    # grab all versions, latest first.
    stored_versions = warehouse.retrieve_versions(file_key, metadata_only=True)
    for i, version in enumerate(reversed(source_versions)):
        stored = next(stored_versions)
        assert isinstance(stored, dict)
        assert warehouse.get_primary_key(stored) == file_key
        assert warehouse.get_source_version(stored) == version


def test_retrieve_non_existent_files(warehouse):
    # returns multiple versions of a file (loaded from a test file, not in warehouse)
    file_key, files = _load_file_versions(warehouse)

    # Store then retrieve the file, this works
    response = warehouse.store(files[0])
    assert response["primary_key"] == file_key
    assert response["status_code"] == warehouse.STATUS.SUCCESS
    stored = warehouse.retrieve(file_key)
    assert warehouse.get_primary_key(stored.metadata) == file_key

    # invalid version
    with pytest.raises(exceptions.OperationError):
        warehouse.retrieve(file_key, "some-random-version")

    # retrieve a non-existent file
    key = ("http://some-random-key",)
    stored = warehouse.retrieve(key)
    assert stored is None
    stored_versions = warehouse.retrieve_versions(key)
    with pytest.raises(StopIteration):
        next(stored_versions)


def test_retrieve_invalid_keys(warehouse):
    # returns multiple versions of a file (loaded from a test file, not in warehouse)
    file_key, files = _load_file_versions(warehouse)

    # retrieve a non-existent file
    key = ("http://some-random-key",)
    stored = warehouse.retrieve(key)
    assert stored is None

    # retrieve invalid key type
    key = (1234567890,)
    with pytest.raises(exceptions.ArgumentError):
        stored = warehouse.retrieve(key)

    # retrieve invalid key len
    key = ("http://some-random-key", "http://some-random-key")
    with pytest.raises(exceptions.ArgumentError):
        stored = warehouse.retrieve(key)

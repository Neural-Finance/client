#
import re
import os
import time
import shutil
import requests
from six.moves.urllib.parse import urlparse, quote

from wandb.compat import tempfile as compat_tempfile
from wandb import env
from wandb.interface.artifacts import *
from wandb.internal.progress import Progress
from wandb.apis import InternalApi
from wandb.errors.error import CommError
from wandb import util
from wandb.errors.term import termwarn, termlog

# This makes the first sleep 1s, and then doubles it up to total times,
# which makes for ~18 hours.
_REQUEST_RETRY_STRATEGY = requests.packages.urllib3.util.retry.Retry(
    backoff_factor=1,
    total=16,
    status_forcelist=(308, 408, 409, 429, 500, 502, 503, 504),
)

_REQUEST_POOL_CONNECTIONS = 64

_REQUEST_POOL_MAXSIZE = 64


class ArtifactsCache(object):
    def __init__(self, cache_dir):
        self._cache_dir = cache_dir
        util.mkdir_exists_ok(self._cache_dir)
        self._md5_obj_dir = os.path.join(self._cache_dir, "obj", "md5")
        self._etag_obj_dir = os.path.join(self._cache_dir, "obj", "etag")

    def check_md5_obj_path(self, b64_md5, size):
        hex_md5 = util.bytes_to_hex(base64.b64decode(b64_md5))
        path = os.path.join(self._cache_dir, "obj", "md5", hex_md5[:2], hex_md5[2:])
        if os.path.isfile(path) and os.path.getsize(path) == size:
            return path, True
        util.mkdir_exists_ok(os.path.dirname(path))
        return path, False

    def check_etag_obj_path(self, etag, size):
        path = os.path.join(self._cache_dir, "obj", "etag", etag[:2], etag[2:])
        if os.path.isfile(path) and os.path.getsize(path) == size:
            return path, True
        util.mkdir_exists_ok(os.path.dirname(path))
        return path, False


_artifacts_cache = None


def get_artifacts_cache():
    global _artifacts_cache
    if _artifacts_cache is None:
        cache_dir = os.path.join(env.get_cache_dir(), "artifacts")
        _artifacts_cache = ArtifactsCache(cache_dir)
    return _artifacts_cache


class Artifact(object):
    """An artifact object you can write files into, and pass to log_artifact."""

    def __init__(self, name, type, description=None, metadata=None):
        if not re.match("^[a-zA-Z0-9_\-.]+$", name):
            raise ValueError(
                'Artifact name may only contain alphanumeric characters, dashes, underscores, and dots. Invalid name: "%s"'
                % name
            )
        # TODO: this shouldn't be a property of the artifact. It's a more like an
        # argument to log_artifact.
        storage_layout = StorageLayout.V2
        if env.get_use_v1_artifacts():
            storage_layout = StorageLayout.V1

        self._storage_policy = WandbStoragePolicy(
            config={
                "storageLayout": storage_layout,
                #  TODO: storage region
            }
        )
        self._api = InternalApi()
        self._final = False
        self._digest = None
        self._file_entries = None
        self._manifest = ArtifactManifestV1(self, self._storage_policy)
        self._cache = get_artifacts_cache()
        self._added_new = False
        # You can write into this directory when creating artifact files
        self._artifact_dir = compat_tempfile.TemporaryDirectory(
            missing_ok_on_cleanup=True
        )
        self.type = type
        self.name = name
        self.description = description
        self.metadata = metadata

    @property
    def id(self):
        # The artifact hasn't been saved so an ID doesn't exist yet.
        return None

    @property
    def entity(self):
        # TODO: querying for default entity a good idea here?
        return self._api.settings("entity") or self._api.viewer().get("entity")

    @property
    def project(self):
        return self._api.settings("project")

    @property
    def manifest(self):
        self.finalize()
        return self._manifest

    @property
    def digest(self):
        self.finalize()
        # Digest will be none if the artifact hasn't been saved yet.
        return self._digest

    def _ensure_can_add(self):
        if self._final:
            raise ValueError("Can't add to finalized artifact.")

    def new_file(self, name, mode="w"):
        self._ensure_can_add()
        path = os.path.join(self._artifact_dir.name, name.lstrip("/"))
        if os.path.exists(path):
            raise ValueError('File with name "%s" already exists' % name)
        util.mkdir_exists_ok(os.path.dirname(path))
        self._added_new = True
        return open(path, mode)

    def add_file(self, local_path, name=None):
        self._ensure_can_add()
        if not os.path.isfile(local_path):
            raise ValueError("Path is not a file: %s" % local_path)

        name = name or os.path.basename(local_path)
        entry = ArtifactManifestEntry(
            name,
            None,
            digest=md5_file_b64(local_path),
            size=os.path.getsize(local_path),
            local_path=local_path,
        )
        self._manifest.add_entry(entry)

    def add_dir(self, local_path, name=None):
        self._ensure_can_add()
        if not os.path.isdir(local_path):
            raise ValueError("Path is not a directory: %s" % local_path)

        termlog(
            "Adding directory to artifact (%s)... "
            % os.path.join(".", os.path.normpath(local_path)),
            newline=False,
        )
        start_time = time.time()

        paths = []
        for dirpath, _, filenames in os.walk(local_path, followlinks=True):
            for fname in filenames:
                physical_path = os.path.join(dirpath, fname)
                logical_path = os.path.relpath(physical_path, start=local_path)
                if name is not None:
                    logical_path = os.path.join(name, logical_path)
                paths.append((logical_path, physical_path))

        def add_manifest_file(log_phy_path):
            logical_path, physical_path = log_phy_path
            self._manifest.add_entry(
                ArtifactManifestEntry(
                    logical_path,
                    None,
                    digest=md5_file_b64(physical_path),
                    size=os.path.getsize(physical_path),
                    local_path=physical_path,
                )
            )

        import multiprocessing.dummy  # this uses threads

        NUM_THREADS = 8
        pool = multiprocessing.dummy.Pool(NUM_THREADS)
        pool.map(add_manifest_file, paths)
        pool.close()
        pool.join()

        termlog("Done. %.1fs" % (time.time() - start_time), prefix=False)

    def add_reference(self, uri, name=None, checksum=True, max_objects=None):
        url = urlparse(uri)
        if not url.scheme:
            raise ValueError(
                "References must be URIs. To reference a local file, use file://"
            )
        if self._final:
            raise ValueError("Can't add to finalized artifact.")
        manifest_entries = self._storage_policy.store_reference(
            self, uri, name=name, checksum=checksum, max_objects=max_objects
        )
        for entry in manifest_entries:
            self._manifest.add_entry(entry)

    def get_path(self, name):
        raise ValueError("Cannot load paths from an artifact before it has been saved")

    def download(self):
        raise ValueError("Cannot call download on an artifact before it has been saved")

    def finalize(self):
        if self._final:
            return self._file_entries

        # Record any created files in the manifest.
        if self._added_new:
            self.add_dir(self._artifact_dir.name)

        # mark final after all files are added
        self._final = True
        self._digest = self._manifest.digest()

        # If there are new files, move them into the artifact cache now. Our temp
        # self._artifact_dir may not be available by the time file pusher syncs
        # these files.
        if self._added_new:
            # Update the file entries for new files to point at their new location.
            def remap_entry(entry):
                if entry.local_path is None or not entry.local_path.startswith(
                    self._artifact_dir.name
                ):
                    return entry
                rel_path = os.path.relpath(
                    entry.local_path, start=self._artifact_dir.name
                )
                local_path = os.path.join(self._artifact_dir.name, rel_path)
                cache_path, hit = self._cache.check_md5_obj_path(
                    entry.digest, entry.size
                )
                if not hit:
                    shutil.copyfile(local_path, cache_path)
                entry.local_path = cache_path

            for entry in self._manifest.entries.values():
                remap_entry(entry)


class ArtifactManifestV1(ArtifactManifest):
    @classmethod
    def version(cls):
        return 1

    @classmethod
    def from_manifest_json(cls, artifact, manifest_json):
        if manifest_json["version"] != cls.version():
            raise ValueError(
                "Expected manifest version 1, got %s" % manifest_json["version"]
            )

        storage_policy_name = manifest_json["storagePolicy"]
        storage_policy_config = manifest_json.get("storagePolicyConfig", {})
        storage_policy_cls = StoragePolicy.lookup_by_name(storage_policy_name)
        if storage_policy_cls is None:
            raise ValueError('Failed to find storage policy "%s"' % storage_policy_name)

        entries = {
            name: ArtifactManifestEntry(
                path=name,
                digest=val["digest"],
                birth_artifact_id=val.get("birthArtifactID"),
                ref=val.get("ref"),
                size=val.get("size"),
                extra=val.get("extra"),
                local_path=val.get("local_path"),
            )
            for name, val in manifest_json["contents"].items()
        }

        return cls(
            artifact, storage_policy_cls.from_config(storage_policy_config), entries
        )

    def __init__(self, artifact, storage_policy, entries=None):
        super(ArtifactManifestV1, self).__init__(
            artifact, storage_policy, entries=entries
        )

    def to_manifest_json(self):
        """This is the JSON that's stored in wandb_manifest.json

        If include_local is True we also include the local paths to files. This is
        used to represent an artifact that's waiting to be saved on the current
        system. We don't need to include the local paths in the artifact manifest
        contents.
        """
        contents = {}
        for entry in sorted(self.entries.values(), key=lambda k: k.path):
            json_entry = {
                "digest": entry.digest,
            }
            if entry.birth_artifact_id:
                json_entry["birthArtifactID"] = entry.birth_artifact_id
            if entry.ref:
                json_entry["ref"] = entry.ref
            if entry.extra:
                json_entry["extra"] = entry.extra
            if entry.size is not None:
                json_entry["size"] = entry.size
            contents[entry.path] = json_entry
        return {
            "version": self.__class__.version(),
            "storagePolicy": self.storage_policy.name(),
            "storagePolicyConfig": self.storage_policy.config() or {},
            "contents": contents,
        }

    def digest(self):
        hasher = hashlib.md5()
        hasher.update("wandb-artifact-manifest-v1\n".encode())
        for (name, entry) in sorted(self.entries.items(), key=lambda kv: kv[0]):
            hasher.update("{}:{}\n".format(name, entry.digest).encode())
        return hasher.hexdigest()


class ArtifactManifestEntry(object):
    def __init__(
        self,
        path,
        ref,
        digest,
        birth_artifact_id=None,
        size=None,
        extra=None,
        local_path=None,
    ):
        if local_path is not None and size is None:
            raise AssertionError(
                "programming error, size required when local_path specified"
            )
        self.path = path
        self.ref = ref  # This is None for files stored in the artifact.
        self.digest = digest
        self.birth_artifact_id = birth_artifact_id
        self.size = size
        self.extra = extra or {}
        # This is not stored in the manifest json, it's only used in the process
        # of saving
        self.local_path = local_path

    def __repr__(self):
        if self.ref is not None:
            summary = "ref: %s/%s" % (self.ref, self.path)
        else:
            summary = "digest: %s" % self.digest

        return "<ManifestEntry %s>" % summary


class WandbStoragePolicy(StoragePolicy):
    @classmethod
    def name(cls):
        return "wandb-storage-policy-v1"

    @classmethod
    def from_config(cls, config):
        return cls(config=config)

    def __init__(self, config=None):
        self._cache = get_artifacts_cache()
        self._config = config or {}
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            max_retries=_REQUEST_RETRY_STRATEGY,
            pool_connections=_REQUEST_POOL_CONNECTIONS,
            pool_maxsize=_REQUEST_POOL_MAXSIZE,
        )
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

        s3 = S3Handler()
        gcs = GCSHandler()
        http = HTTPHandler(self._session)
        https = HTTPHandler(self._session, scheme="https")
        file_handler = LocalFileHandler()

        self._api = InternalApi()
        self._handler = MultiHandler(
            handlers=[s3, gcs, http, https, file_handler,],
            default_handler=TrackingHandler(),
        )

    def config(self):
        return self._config

    def load_file(self, artifact, name, manifest_entry):
        path, hit = self._cache.check_md5_obj_path(
            manifest_entry.digest, manifest_entry.size
        )
        if hit:
            return path

        response = self._session.get(
            self._file_url(self._api, artifact.entity, manifest_entry),
            auth=("api", self._api.api_key),
            stream=True,
        )
        response.raise_for_status()

        with open(path, "wb") as file:
            for data in response.iter_content(chunk_size=16 * 1024):
                file.write(data)
        return path

    def store_reference(
        self, artifact, path, name=None, checksum=True, max_objects=None
    ):
        return self._handler.store_path(
            artifact, path, name=name, checksum=checksum, max_objects=max_objects
        )

    def load_reference(self, artifact, name, manifest_entry, local=False):
        return self._handler.load_path(self._cache, manifest_entry, local)

    def _file_url(self, api, entity_name, manifest_entry):
        storage_layout = self._config.get("storageLayout", StorageLayout.V1)
        storage_region = self._config.get("storageRegion", "default")
        md5_hex = util.bytes_to_hex(base64.b64decode(manifest_entry.digest))

        if storage_layout == StorageLayout.V1:
            return "{}/artifacts/{}/{}".format(
                api.settings("base_url"), entity_name, md5_hex
            )
        elif storage_layout == StorageLayout.V2:
            return "{}/artifactsV2/{}/{}/{}/{}".format(
                api.settings("base_url"),
                storage_region,
                entity_name,
                quote(manifest_entry.birth_artifact_id),
                md5_hex,
            )
        else:
            raise Exception("unrecognized storage layout: {}".format(storage_layout))

    def store_file(self, artifact_id, entry, preparer, progress_callback=None):
        # write-through cache
        cache_path, hit = self._cache.check_md5_obj_path(entry.digest, entry.size)
        if not hit:
            shutil.copyfile(entry.local_path, cache_path)

        resp = preparer.prepare(
            lambda: {
                "artifactID": artifact_id,
                "name": entry.path,
                "md5": entry.digest,
            }
        )

        entry.birth_artifact_id = resp.birth_artifact_id
        exists = resp.upload_url is None
        if not exists:
            with open(entry.local_path, "rb") as file:
                # This fails if we don't send the first byte before the signed URL
                # expires.
                r = self._session.put(
                    resp.upload_url,
                    headers={
                        header.split(":", 1)[0]: header.split(":", 1)[1]
                        for header in (resp.upload_headers or {})
                    },
                    data=Progress(file, callback=progress_callback),
                )
                r.raise_for_status()
        return exists


# Don't use this yet!
class __S3BucketPolicy(StoragePolicy):
    @classmethod
    def name(cls):
        return "wandb-s3-bucket-policy-v1"

    @classmethod
    def from_config(cls, config):
        if "bucket" not in config:
            raise ValueError("Bucket name not found in config")
        return cls(config["bucket"])

    def __init__(self, bucket):
        self._bucket = bucket
        s3 = S3Handler(bucket)
        local = LocalFileHandler()

        self._handler = MultiHandler(
            handlers=[s3, local,], default_handler=TrackingHandler()
        )

    def config(self):
        return {"bucket": self._bucket}

    def load_path(self, artifact, manifest_entry, local=False):
        return self._handler.load_path(artifact, manifest_entry, local=local)

    def store_path(self, artifact, path, name=None, checksum=True, max_objects=None):
        return self._handler.store_path(
            artifact, path, name=name, checksum=checksum, max_objects=max_objects
        )


class MultiHandler(StorageHandler):
    def __init__(self, handlers=None, default_handler=None):
        self._handlers = {}
        self._default_handler = default_handler

        handlers = handlers or []
        for handler in handlers:
            self._handlers[handler.scheme] = handler

    @property
    def scheme(self):
        raise NotImplementedError()

    def load_path(self, artifact, manifest_entry, local=False):
        url = urlparse(manifest_entry.ref)
        if url.scheme not in self._handlers:
            if self._default_handler is not None:
                return self._default_handler.load_path(
                    artifact, manifest_entry, local=local
                )
            raise ValueError(
                'No storage handler registered for scheme "%s"' % url.scheme
            )
        return self._handlers[url.scheme].load_path(
            artifact, manifest_entry, local=local
        )

    def store_path(self, artifact, path, name=None, checksum=True, max_objects=None):
        url = urlparse(path)
        if url.scheme not in self._handlers:
            if self._handlers is not None:
                return self._default_handler.store_path(
                    artifact,
                    path,
                    name=name,
                    checksum=checksum,
                    max_objects=max_objects,
                )
            raise ValueError(
                'No storage handler registered for scheme "%s"' % url.scheme
            )
        return self._handlers[url.scheme].store_path(
            artifact, path, name=name, checksum=checksum, max_objects=max_objects
        )


class TrackingHandler(StorageHandler):
    def __init__(self, scheme=None):
        """
        Tracks paths as is, with no modification or special processing. Useful
        when paths being tracked are on file systems mounted at a standardized
        location.

        For example, if the data to track is located on an NFS share mounted on
        /data, then it is sufficient to just track the paths.
        """
        self._scheme = scheme

    @property
    def scheme(self):
        return self._scheme

    def load_path(self, artifact, manifest_entry, local=False):
        if local:
            # Likely a user error. The tracking handler is
            # oblivious to the underlying paths, so it has
            # no way of actually loading it.
            url = urlparse(manifest_entry.ref)
            raise ValueError(
                "Cannot download file at path %s, scheme %s not recognized"
                % (manifest_entry.ref, url.scheme)
            )
        return manifest_entry.path

    def store_path(self, artifact, path, name=None, checksum=False, max_objects=None):
        url = urlparse(path)
        if name is None:
            raise ValueError(
                'You must pass name="<entry_name>" when tracking references with unknown schemes. ref: %s'
                % path
            )
        termwarn(
            "Artifact references with unsupported schemes cannot be checksummed: %s"
            % path
        )
        name = name or url.path[1:]  # strip leading slash
        return [ArtifactManifestEntry(name, path, digest=path)]


DEFAULT_MAX_OBJECTS = 10000


class LocalFileHandler(StorageHandler):

    """Handles file:// references"""

    def __init__(self, scheme=None):
        """
        Tracks files or directories on a local filesystem. Directories
        are expanded to create an entry for each file contained within.
        """
        self._scheme = scheme or "file"
        self._cache = get_artifacts_cache()

    @property
    def scheme(self):
        return self._scheme

    def load_path(self, artifact, manifest_entry, local=False):
        url = urlparse(manifest_entry.ref)
        local_path = "%s%s" % (url.netloc, url.path)
        if not os.path.exists(local_path):
            raise ValueError(
                "Local file reference: Failed to find file at path %s" % local_path
            )

        path, hit = self._cache.check_md5_obj_path(
            manifest_entry.digest, manifest_entry.size
        )
        if hit:
            return path

        md5 = md5_file_b64(local_path)
        if md5 != manifest_entry.digest:
            raise ValueError(
                "Local file reference: Digest mismatch for path %s: expected %s but found %s"
                % (local_path, manifest_entry.digest, md5)
            )

        util.mkdir_exists_ok(os.path.dirname(path))
        shutil.copy(local_path, path)
        return path

    def store_path(self, artifact, path, name=None, checksum=True, max_objects=None):
        url = urlparse(path)
        local_path = "%s%s" % (url.netloc, url.path)
        max_objects = max_objects or DEFAULT_MAX_OBJECTS
        # We have a single file or directory
        # Note, we follow symlinks for files contained within the directory
        entries = []
        if checksum == False:
            return [
                ArtifactManifestEntry(name or os.path.basename(path), path, digest=path)
            ]

        if os.path.isdir(local_path):
            i = 0
            start_time = time.time()
            termlog(
                'Generating checksum for up to %i files in "%s"...'
                % (max_objects, local_path),
                newline=False,
            )
            for root, dirs, files in os.walk(local_path):
                for sub_path in files:
                    i += 1
                    if i >= max_objects:
                        raise ValueError(
                            "Exceeded %i objects tracked, pass max_objects to add_reference"
                            % max_objects
                        )
                    entry = ArtifactManifestEntry(
                        os.path.basename(sub_path),
                        os.path.join(path, sub_path),
                        size=os.path.getsize(sub_path),
                        digest=md5_file_b64(sub_path),
                    )
                    entries.append(entry)
            termlog("Done. %.1fs" % (time.time() - start_time), prefix=False)
        elif os.path.isfile(local_path):
            name = name or os.path.basename(local_path)
            entry = ArtifactManifestEntry(
                name,
                path,
                size=os.path.getsize(local_path),
                digest=md5_file_b64(local_path),
            )
            entries.append(entry)
        else:
            # TODO: update error message if we don't allow directories.
            raise ValueError('Path "%s" must be a valid file or directory path' % path)
        return entries


class S3Handler(StorageHandler):
    def __init__(self, scheme=None):
        self._scheme = scheme or "s3"
        self._s3 = None
        self._versioning_enabled = None
        self._cache = get_artifacts_cache()

    @property
    def scheme(self):
        return self._scheme

    def init_boto(self):
        if self._s3 is not None:
            return self._s3
        boto3 = util.get_module(
            "boto3",
            required="s3:// references requires the boto3 library, run pip install wandb[aws]",
        )
        self._s3 = boto3.session.Session().resource(
            "s3",
            endpoint_url=os.getenv("AWS_S3_ENDPOINT_URL"),
            region_name=os.getenv("AWS_REGION"),
        )
        self._botocore = util.get_module("botocore")
        return self._s3

    def _parse_uri(self, uri):
        url = urlparse(uri)
        bucket = url.netloc
        key = url.path[1:]  # strip leading slash
        return bucket, key

    def versioning_enabled(self, bucket):
        self.init_boto()
        if self._versioning_enabled is not None:
            return self._versioning_enabled
        res = self._s3.BucketVersioning(bucket)
        self._versioning_enabled = res.status == "Enabled"
        return self._versioning_enabled

    def load_path(self, artifact, manifest_entry, local=False):
        path, hit = self._cache.check_etag_obj_path(
            manifest_entry.digest, manifest_entry.size
        )
        if hit:
            return path

        self.init_boto()
        bucket, key = self._parse_uri(manifest_entry.ref)
        version = manifest_entry.extra.get("versionID")

        extra_args = {}
        if version is None:
            # We don't have version information so just get the latest version
            # and fallback to listing all versions if we don't have a match.
            obj = self._s3.Object(bucket, key)
            etag = self._etag_from_obj(obj)
            if etag != manifest_entry.digest:
                if self.versioning_enabled(bucket):
                    # Fallback to listing versions
                    obj = None
                    object_versions = self._s3.Bucket(bucket).object_versions.filter(
                        Prefix=key
                    )
                    for object_version in object_versions:
                        if (
                            manifest_entry.extra.get("etag")
                            == object_version.e_tag[1:-1]
                        ):
                            obj = object_version.Object()
                            extra_args["VersionId"] = object_version.version_id
                            break
                    if obj is None:
                        raise ValueError(
                            "Couldn't find object version for %s/%s matching etag %s"
                            % (self._bucket, key, manifest_entry.extra.get("etag"))
                        )
                else:
                    raise ValueError(
                        "Digest mismatch for object %s: expected %s but found %s"
                        % (manifest_entry.ref, manifest_entry.digest, etag)
                    )
        else:
            obj = self._s3.ObjectVersion(bucket, key, version).Object()
            extra_args["VersionId"] = version

        if not local:
            return manifest_entry.ref

        obj.download_file(path, ExtraArgs=extra_args)
        return path

    def store_path(self, artifact, path, name=None, checksum=True, max_objects=None):
        self.init_boto()
        bucket, key = self._parse_uri(path)
        max_objects = max_objects or DEFAULT_MAX_OBJECTS
        if not checksum:
            return [ArtifactManifestEntry(name or key, path, digest=path)]

        objs = [self._s3.Object(bucket, key)]
        start_time = None
        multi = False
        try:
            objs[0].load()
        except self._botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                multi = True
                start_time = time.time()
                termlog(
                    'Generating checksum for up to %i objects with prefix "%s"... '
                    % (max_objects, key),
                    newline=False,
                )
                objs = (
                    self._s3.Bucket(bucket)
                    .objects.filter(Prefix=key)
                    .limit(max_objects)
                )
            else:
                raise CommError(
                    "Unable to connect to S3 (%s): %s"
                    % (e.response["Error"]["Code"], e.response["Error"]["Message"])
                )

        # Weird iterator scoping makes us assign this to a local function
        size = self._size_from_obj
        entries = [
            self._entry_from_obj(obj, path, name, prefix=key, multi=multi)
            for obj in objs
            if size(obj) > 0
        ]
        if start_time is not None:
            termlog("Done. %.1fs" % (time.time() - start_time), prefix=False)
        if len(entries) >= max_objects:
            raise ValueError(
                "Exceeded %i objects tracked, pass max_objects to add_reference"
                % max_objects
            )
        return entries

    def _size_from_obj(self, obj):
        # ObjectSummary has size, Object has content_length
        if hasattr(obj, "size"):
            size = obj.size
        else:
            size = obj.content_length
        return size

    def _entry_from_obj(self, obj, path, name=None, prefix="", multi=False):
        ref = path
        if name is None:
            if prefix in obj.key and prefix != obj.key:
                relpath = os.path.relpath(obj.key, start=prefix)
                name = relpath
                ref = os.path.join(path, relpath)
            else:
                name = os.path.basename(obj.key)
                ref = path
        elif multi:
            relpath = os.path.relpath(obj.key, start=prefix)
            name = os.path.join(name, relpath)
            ref = os.path.join(path, relpath)
        return ArtifactManifestEntry(
            name,
            ref,
            self._etag_from_obj(obj),
            size=self._size_from_obj(obj),
            extra=self._extra_from_obj(obj),
        )

    @staticmethod
    def _etag_from_obj(obj):
        return obj.e_tag[1:-1]  # escape leading and trailing quote

    @staticmethod
    def _extra_from_obj(obj):
        extra = {
            "etag": obj.e_tag[1:-1],  # escape leading and trailing quote
        }
        # ObjectSummary will never have version_id
        if hasattr(obj, "version_id") and obj.version_id != "null":
            extra["versionID"] = obj.version_id
        return extra

    @staticmethod
    def _content_addressed_path(md5):
        # TODO: is this the structure we want? not at all human
        # readable, but that's probably OK. don't want people
        # poking around in the bucket
        return "wandb/%s" % base64.b64encode(md5.encode("ascii")).decode("ascii")


class GCSHandler(StorageHandler):
    def __init__(self, scheme=None):
        self._scheme = scheme or "gs"
        self._client = None
        self._versioning_enabled = None
        self._cache = get_artifacts_cache()

    def versioning_enabled(self, bucket):
        if self._versioning_enabled is not None:
            return self._versioning_enabled
        self.init_gcs()
        bucket = self._client.bucket(bucket)
        bucket.reload()
        self._versioning_enabled = bucket.versioning_enabled
        return self._versioning_enabled

    @property
    def scheme(self):
        return self._scheme

    def init_gcs(self):
        if self._client is not None:
            return self._client
        storage = util.get_module(
            "google.cloud.storage",
            required="gs:// references requires the google-cloud-storage library, run pip install wandb[gcp]",
        )
        self._client = storage.Client()
        return self._client

    def _parse_uri(self, uri):
        url = urlparse(uri)
        bucket = url.netloc
        key = url.path[1:]
        return bucket, key

    def load_path(self, artifact, manifest_entry, local=False):
        path, hit = self._cache.check_md5_obj_path(
            manifest_entry.digest, manifest_entry.size
        )
        if hit:
            return path

        self.init_gcs()
        bucket, key = self._parse_uri(manifest_entry.ref)
        version = manifest_entry.extra.get("versionID")

        extra_args = {}
        obj = None
        # First attempt to get the generation specified, this will return None if versioning is not enabled
        if version is not None:
            obj = self._client.bucket(bucket).get_blob(key, generation=version)

        if obj is None:
            # Object versioning is disabled on the bucket, so just get
            # the latest version and make sure the MD5 matches.
            obj = self._client.bucket(bucket).get_blob(key)
            if obj is None:
                raise ValueError(
                    "Unable to download object %s with generation %s"
                    % (manifest_entry.ref, version)
                )
            md5 = obj.md5_hash
            if md5 != manifest_entry.digest:
                raise ValueError(
                    "Digest mismatch for object %s: expected %s but found %s"
                    % (manifest_entry.ref, manifest_entry.digest, md5)
                )

        if not local:
            return manifest_entry.ref

        obj.download_to_filename(path)
        return path

    def store_path(self, artifact, path, name=None, checksum=True, max_objects=None):
        self.init_gcs()
        bucket, key = self._parse_uri(path)
        max_objects = max_objects or DEFAULT_MAX_OBJECTS

        if checksum == False:
            return [ArtifactManifestEntry(name or key, path, digest=path)]
        start_time = None
        obj = self._client.bucket(bucket).get_blob(key)
        multi = obj is None
        if multi:
            start_time = time.time()
            termlog(
                'Generating checksum for up to %i objects with prefix "%s"... '
                % (max_objects, key),
                newline=False,
            )
            objects = self._client.bucket(bucket).list_blobs(
                prefix=key, max_results=max_objects
            )
        else:
            objects = [obj]

        entries = [
            self._entry_from_obj(obj, path, name, prefix=key, multi=multi)
            for obj in objects
        ]
        if start_time is not None:
            termlog("Done. %.1fs" % (time.time() - start_time), prefix=False)
        if len(entries) >= max_objects:
            raise ValueError(
                "Exceeded %i objects tracked, pass max_objects to add_reference"
                % max_objects
            )
        return entries

    def _entry_from_obj(self, obj, path, name=None, prefix="", multi=False):
        ref = path
        if name is None:
            if prefix in obj.name and prefix != obj.name:
                name = os.path.relpath(obj.name, start=prefix)
                ref = os.path.join(path, name)
            else:
                name = os.path.basename(obj.name)
        elif multi:
            # We're listing a path and user provided name, just prepend it
            name = os.path.join(name, os.path.basename(obj.name))
            ref = os.path.join(path, name)
        return ArtifactManifestEntry(
            name, ref, obj.md5_hash, size=obj.size, extra=self._extra_from_obj(obj)
        )

    @staticmethod
    def _extra_from_obj(obj):
        return {
            "etag": obj.etag,
            "versionID": obj.generation,
        }

    @staticmethod
    def _content_addressed_path(md5):
        # TODO: is this the structure we want? not at all human
        # readable, but that's probably OK. don't want people
        # poking around in the bucket
        return "wandb/%s" % base64.b64encode(md5.encode("ascii")).decode("ascii")


class HTTPHandler(StorageHandler):
    def __init__(self, session, scheme=None):
        self._scheme = scheme or "http"
        self._cache = get_artifacts_cache()
        self._session = session

    @property
    def scheme(self):
        return self._scheme

    def load_path(self, artifact, manifest_entry, local=False):
        if not local:
            return manifest_entry.ref

        path, hit = self._cache.check_etag_obj_path(
            manifest_entry.digest, manifest_entry.size
        )
        if hit:
            return path

        response = self._session.get(manifest_entry.ref, stream=True)
        response.raise_for_status()

        digest, size, extra = self._entry_from_headers(response.headers)
        if manifest_entry.digest != digest:
            raise ValueError(
                "Digest mismatch for url %s: expected %s but found %s"
                % (manifest_entry.ref, manifest_entry.digest, digest)
            )

        with open(path, "wb") as file:
            for data in response.iter_content(chunk_size=16 * 1024):
                file.write(data)
        return path

    def store_path(self, artifact, path, name=None, checksum=True, max_objects=None):
        name = name or os.path.basename(path)
        if not checksum:
            return [ArtifactManifestEntry(name, path, digest=path)]

        with self._session.get(path, stream=True) as response:
            response.raise_for_status()
            digest, size, extra = self._entry_from_headers(response.headers)
        return [
            ArtifactManifestEntry(name, path, digest=digest, size=size, extra=extra)
        ]

    def _entry_from_headers(self, headers):
        response_headers = {k.lower(): v for k, v in headers.items()}
        size = response_headers.get("content-length", None)
        if size:
            size = int(size)

        digest = response_headers.get("etag", None)  #
        extra = {}
        if digest:
            extra["etag"] = digest
        if digest and digest[:1] == '"' and digest[-1:] == '"':
            digest = digest[1:-1]  # trim leading and trailing quotes around etag
        return digest, size, extra

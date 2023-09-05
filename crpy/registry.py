import io
import json
import os
import pathlib
import re
import tarfile
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Union

from async_lru import alru_cache

from crpy.auth import get_token, get_url_from_auth_header
from crpy.common import (
    Platform,
    Response,
    _request,
    _stream,
    compute_sha256,
    platform_from_dict,
)
from crpy.storage import get_credentials, get_layer_from_cache, save_layer

# taken from https://github.com/davedoesdev/dxf/blob/master/dxf/__init__.py#L24
_schema1_mimetype = "application/vnd.docker.distribution.manifest.v1+json"

_schema2_mimetype = "application/vnd.docker.distribution.manifest.v2+json"
_schema2_list_mimetype = "application/vnd.docker.distribution.manifest.list.v2+json"

# OCIv1 equivalent of a docker registry v2 manifests
_ociv1_manifest_mimetype = "application/vnd.oci.image.manifest.v1+json"
# OCIv1 equivalent of a docker registry v2 "manifests list"
_ociv1_index_mimetype = "application/vnd.oci.image.index.v1+json"


@dataclass
class RegistryInfo:
    """
    See https://containers.gitbook.io/build-containers-the-hard-way/ for an in depth explanation of what is going on.
    """

    registry: str
    repository: str
    tag: str
    https: bool = True
    token: Optional[str] = None

    @property
    def _headers(self) -> dict:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def v2_url(self):
        method = "https" if self.https else "http"
        return f"{method}://{self.registry}/v2"

    def manifest_url(self):
        return f"{self.v2_url()}/{self.repository}/manifests/{self.tag}"

    def blobs_url(self):
        return f"{self.v2_url()}/{self.repository}/blobs"

    def __hash__(self):
        return hash((self.registry, self.registry, self.tag, self.https))

    def __str__(self):
        return f"{self.registry}/{self.repository}:{self.tag}"

    async def auth(self, www_auth: str = None, username: str = None, password: str = None, b64_token: str = None):
        if www_auth is None:
            method = "https" if self.https else "http"
            response = await _request(f"{method}://{self.registry}/v2/", method="get")
            www_auth = response.headers["WWW-Authenticate"]
        assert www_auth.startswith('Bearer realm="')
        # check if config contains username and password we can use
        if not b64_token:
            b64_token = get_credentials(self.registry)
        # reuse this token in consecutive requests
        self.token = await get_token(
            get_url_from_auth_header(www_auth), username=username, password=password, b64_token=b64_token
        )
        print(f"Authenticated at {self}")
        return self.token

    @staticmethod
    def from_url(url: str) -> "RegistryInfo":
        """
        >>> args = RegistryInfo.from_url('index.docker.io/library/nginx')
        >>> args.registry
        'index.docker.io'
        >>> args.repository
        'library/nginx'
        >>> args.tag
        'latest'
        >>> name_parsed = RegistryInfo.from_url('gcr.io/distroless/cc:latest')
        >>> name_parsed.registry
        'gcr.io'
        >>> name_parsed.repository
        'distroless/cc'
        >>> name_parsed.tag
        'latest'
        """
        if "://" in url:
            scheme, url = url.split("://")
            has_scheme = True
        else:
            scheme, has_scheme = "https", False
        possibly_hub_image = (url.count("/") == 0 and "." not in url.split("/")[0]) or (  # example: alpine:latest
            url.count("/") == 1  # example: bitnami/postgres:latest
            and "." not in url.split("/")[0]  # exception: myregistry.com/alpine:latest
            and ":" not in url.split("/")[0]  # exception: localhost:5000/alpine:latest
        )
        if not has_scheme and possibly_hub_image:
            # when user provides a single word like "alpine" or "alpine:latest" or bitnami/postgresql
            registry, repository_raw = "index.docker.io", f"library/{url}" if "/" not in url else url
        else:
            registry, _, repository_raw = url.partition("/")
            if "docker.io" in registry and "/" not in repository_raw:
                # library image
                repository_raw = f"library/{repository_raw}"
        name, tag = (repository_raw.split(":") + ["latest"])[:2]
        return RegistryInfo(registry, name.strip("/"), tag, scheme == "https")

    @alru_cache
    async def get_manifest(self, fat: bool = False) -> Response:
        """
        Gets the manifest for a remote docker image. This is a JSON file containing the metadata for how the image is
        stored.
        :param fat: If it should return the manifest list, rather than the default manifest. This allows the user to
            also select multiple architectures instead of being limited in just the default one.
            See https://docs.docker.com/registry/spec/manifest-v2-2/ for explanation.
        :return: Response object with status code, raw data and response headers.
        """
        if fat:
            headers = {
                "Accept": ", ".join(
                    (
                        _schema1_mimetype,
                        _schema2_mimetype,
                        _schema2_list_mimetype,
                        _ociv1_manifest_mimetype,
                        _ociv1_index_mimetype,
                    )
                )
            }
        else:
            headers = {
                "Accept": ", ".join(
                    (
                        _schema1_mimetype,
                        _schema2_mimetype,
                    )
                )
            }
        response = await _request(self.manifest_url(), headers | self._headers, method="get")
        if response.status == 401:
            www_auth = response.headers["WWW-Authenticate"]
            await self.auth(www_auth)
            response = await _request(self.manifest_url(), headers | self._headers, method="get")
            if response.status == 401:
                raise ValueError(f"Could not authenticate to registry {self}")
        return response

    @alru_cache
    async def get_manifest_from_architecture(self, architecture: Union[str, Platform] = None) -> dict:
        if isinstance(architecture, Platform):
            architecture = architecture.value
        if architecture is not None:
            manifests = (await self.get_manifest(fat=True)).json()
            available_architectures = [platform_from_dict(manifest["platform"]) for manifest in manifests["manifests"]]
            for idx, a in enumerate(available_architectures):
                if a == architecture:
                    return manifests["manifests"][idx]
            raise ValueError(f"No matching manifest for {architecture} in the manifest list entries at {self}")
        else:
            manifest = await self.get_manifest()
            return manifest.json()

    @alru_cache
    async def get_config(self, architecture: Union[str, Platform] = None) -> Response:
        """
        Gets the config of a docker image. The config contains all basic information of a docker image, including the
        entrypoints, cmd, environment variables, etc.

        :param architecture: optional architecture for the image. If not provided, the default registry architecture
            will be pulled.
        :return: Response object with status code, raw data and response headers.
        """
        manifest = await self.get_manifest_from_architecture(architecture)
        config_digest = manifest["config"]["digest"]
        response = await _request(f"{self.blobs_url()}/{config_digest}", self._headers, method="get")
        return response

    @alru_cache
    async def get_layers(self, architecture: Union[str, Platform] = None) -> List[str]:
        """
        Gets the digests for each layer available at the remote registry.
        :param architecture: optional architecture for the image. If not provided, the default registry architecture
            will be pulled.
        :return:
        """
        manifest = await self.get_manifest_from_architecture(architecture)
        layers = [m["digest"] for m in manifest["layers"]]
        return layers

    async def pull_layer(
        self, layer: str, file_obj: Optional[io.BytesIO] = None, use_cache: bool = True
    ) -> Optional[bytes]:
        content = get_layer_from_cache(layer) if use_cache else None

        if content is not None:
            # short-circuit if the content is in the cache
            if file_obj is None:
                return content
            file_obj.write(content)
            return None

        return await self.get_content_from_remote(layer, file_obj, use_cache)

    async def get_content_from_remote(
        self, layer: str, file_obj: Optional[io.BytesIO], use_cache: bool
    ) -> Optional[bytes]:
        content = await self.get_response_content(layer, file_obj)
        if use_cache:
            save_layer(layer, content if file_obj is None else file_obj.getvalue())
        return content

    async def get_response_content(self, layer: str, file_obj: Optional[io.BytesIO]) -> bytes:
        if file_obj is None:
            response = await _request(f"{self.blobs_url()}/{layer}", self._headers, method="get")
            return response.data

        async for chunk in _stream(f"{self.blobs_url()}/{layer}", self._headers):
            file_obj.write(chunk)
        file_obj.seek(0)
        return file_obj.getvalue()

    async def pull(self, output_file: Union[str, pathlib.Path, io.BytesIO], architecture: Union[str, Platform] = None):
        with tempfile.TemporaryDirectory() as temp_dir:
            print(f"{self.tag}: Pulling from {self.registry}/{self.repository}")
            web_manifest = await self.get_manifest_from_architecture(architecture)
            config = await self.get_config(architecture)

            config_filename = f'{web_manifest["config"]["digest"].split(":")[1]}.json'
            with open(f"{temp_dir}/{config_filename}", "wb") as outfile:
                outfile.write(config.data)

            layer_path_l = []
            for layer in await self.get_layers():
                layer_folder = layer.split(":")[-1]
                path = layer_folder + "/layer.tar"
                layer_bytes = await self.pull_layer(layer, use_cache=True)
                os.makedirs(f"{temp_dir}/{layer_folder}", exist_ok=True)
                with open(f"{temp_dir}/{path}", "wb") as f:
                    f.write(layer_bytes)
                layer_path_l.append(path)
                print(f"{layer.split(':')[1][0:12]}: Pull complete")

            manifest = [{"Config": config_filename, "RepoTags": [str(self)], "Layers": layer_path_l}]
            with open(f"{temp_dir}/manifest.json", "w") as outfile:
                json.dump(manifest, outfile)

            if isinstance(output_file, io.BytesIO):
                output_kwargs = {"fileobj": output_file, "mode": "w"}
            else:
                output_kwargs = {"name": output_file, "mode": "w"}
            with tarfile.open(**output_kwargs) as tar_out:
                os.chdir(temp_dir)
                tar_out.add(".")
            print(f"Downloaded image from {self}")

    async def push_layer(self, file_obj: Union[bytes, str, pathlib.Path], force: bool = False) -> Optional[dict]:
        # load layer and compute it's digest
        if isinstance(file_obj, pathlib.Path) or isinstance(file_obj, str):
            with open(file_obj, "rb") as f:
                content = f.read()
        elif isinstance(file_obj, io.BytesIO):
            content = file_obj.read()
        else:
            content = file_obj
        digest = compute_sha256(content)
        manifest = {
            "size": len(content),
            "digest": digest,
        }
        # first check if a blob exists with a HEAD request
        response = await _request(f"{self.blobs_url()}/{digest}", headers=self._headers, method="head")
        if response.status == 401:
            www_auth = response.headers["WWW-Authenticate"].replace("pull", "pull,push")
            await self.auth(www_auth)
            response = await _request(f"{self.blobs_url()}/{digest}", headers=self._headers, method="get")
            if response.status == 401:
                raise ValueError(f"Could not authenticate to registry {self}")
        if response.status == 200 and not force:
            # layer already exists
            manifest["existing"] = True
            return manifest
        # the process for pushing a layer is first making a request to /uploads and getting the location header
        response = await _request(f"{self.blobs_url()}/uploads/", headers=self._headers)
        location_header = response.headers["Location"]
        # we do a monolith upload with a single PUT requests
        response = await _request(
            f"{location_header}&digest={digest}",
            method="put",
            data=content,
            headers=self._headers | {"Content-Type": "application/octet-stream"},
        )
        assert response.status == 201, f"Failed to upload blob with digest {digest}: {response.data}"
        manifest["existing"] = False
        return manifest

    def build_manifest(
        self, config: dict, layers: List[dict], schema_version: int = 2, media_type: str = _schema2_mimetype
    ):
        return {
            "schemaVersion": schema_version,
            "mediaType": media_type,
            "config": config,
            "layers": layers,
        }

    async def push_manifest(self, manifest: dict):
        # build the manifest here according to
        # containers.gitbook.io/build-containers-the-hard-way/#registry-format-docker-image-manifest-v-2-schema-2
        response = await _request(
            f"{self.manifest_url()}",
            method="put",
            data=json.dumps(manifest, indent=3).encode(),
            headers=self._headers | {"Content-Type": _schema2_mimetype},
        )
        assert response.status == 201
        return response

    async def push(self, input_file: Union[str, pathlib.Path, io.BytesIO]):
        try:
            if isinstance(input_file, io.BytesIO):
                t = tarfile.TarFile(fileobj=input_file)
            else:
                t = tarfile.TarFile(input_file)
        except tarfile.ReadError:
            raise ValueError(f"Failed to load {input_file}. Is an Docker image?")
        with tempfile.TemporaryDirectory() as temp_dir:
            t.extractall(temp_dir)
            manifest_path = pathlib.Path(temp_dir) / "manifest.json"
            manifest_content = manifest_path.read_text()
            manifest = json.loads(manifest_content)[-1]
            layers = manifest["Layers"] if "Layers" in manifest else manifest["layers"]

            print(f"The push refers to repository [{self}]")

            # upload config
            config = manifest["Config"] if "Config" in manifest else manifest["config"]
            config_path = pathlib.Path(temp_dir) / config
            config_manifest = await self.push_layer(config_path)
            config_manifest.pop("existing")
            config_manifest["mediaType"] = "application/vnd.docker.container.image.v1+json"

            # upload layers
            layers_manifest = []
            for layer in layers:
                layer_path = pathlib.Path(temp_dir) / layer
                layer_manifest = await self.push_layer(layer_path)
                if not layer_manifest["existing"]:
                    print(f"{layer[0:12]}: Pushed")
                else:
                    print(f"{layer[0:12]}: Layer already exists")
                layer_manifest.pop("existing")
                layer_manifest["mediaType"] = "application/vnd.docker.image.rootfs.diff.tar.gzip"
                layers_manifest.append(layer_manifest)
            # once the blobs are committed, we can push the manifest
            image_manifest = self.build_manifest(config_manifest, layers_manifest)
            r = await self.push_manifest(image_manifest)
            # some registries like docker hub return the header in lower case
            image_digest = r.headers.get("Docker-Content-Digest", "") or r.headers.get("docker-content-digest")
            print(f"Pushed {self.tag}: digest: {image_digest}")

    async def _list(self, path: str, last: str = None, n: int = None, lazy: bool = False) -> List[dict]:
        url = f"{self.v2_url()}/{path}"
        params = {}
        if n is not None:
            params["n"] = n
        if last is not None:
            params["last"] = last
        response = await _request(url, params=params, headers=self._headers, method="get")
        if response.status == 401:
            www_auth = response.headers["WWW-Authenticate"]
            await self.auth(www_auth)
            response = await _request(url, params=params, headers=self._headers, method="get")
            if response.status == 401:
                raise ValueError(f"Could not authenticate to registry {self}")
        ret_value = [response.json()]
        # use pagination to get further tags, if any
        if "Link" in response.headers and not lazy:
            last = re.search(r"last=(\w+)", response.headers["Link"]).group(1)
            n = re.search(r"n=(\d+)", response.headers["Link"]).group(1)
            next_response = await self._list(path, last, int(n))
            ret_value.append(next_response[0])
        return ret_value

    async def list_repositories(self, last: str = None, n: int = None, lazy: bool = False) -> List[str]:
        """
        Lists the repositories contents to show all available images. It will retrieve all pages, unless lazy is
        specified. In order to list, the user token must have the appropriate permissions.

        :param last: Last element received on the previous page, in case of a paged call. ``None`` means from beginning.
        :param n: Number of elements on each page. ``None`` means default from registry.
        :param lazy: If lazy retrieval should be used. In this case, the
        :return: List of repositories available in the registry.
        """
        response = await self._list("_catalog", last, n, lazy)
        return [entry for page in response for entry in page["repositories"]]

    async def list_tags(self, last: str = None, n: int = None, lazy: bool = False) -> List[str]:
        """
        Lists the tags available for the repository. It will retrieve all pages, unless lazy is
        specified. In order to list, the user token must have the appropriate permissions.

        :param last: Last element received on the previous page, in case of a paged call. ``None`` means from beginning.
        :param n: Number of elements on each page. ``None`` means default from registry.
        :param lazy: If lazy retrieval should be used. In this case, the
        :return: List of tags available in the repository.
        """
        response = await self._list(f"{self.repository}/tags/list", last, n, lazy)
        return [entry for page in response for entry in page["tags"]]
"""Zenodo 上传器测试：mock requests，验证 create/upload/publish 与 dry-run 零请求。"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.release import zenodo


def _response(payload, status=200):
    response = mock.Mock()
    response.status_code = status
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class ApiFlowTest(unittest.TestCase):
    def test_create_deposit_payload_and_url(self):
        metadata = {"title": "T", "upload_type": "dataset"}
        with mock.patch.object(zenodo.requests, "post",
                               return_value=_response({"id": 42, "links": {"bucket": "https://b"}})) as post:
            deposition = zenodo.create_deposit(metadata, "tok", sandbox=True)
        self.assertEqual(deposition["id"], 42)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://sandbox.zenodo.org/api/deposit/depositions")
        self.assertEqual(kwargs["json"], {"metadata": metadata})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")

    def test_upload_files_flattens_paths(self):
        with TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "02_compilation").mkdir()
            (package / "README.md").write_text("r")
            (package / "02_compilation" / "observations.csv").write_text("a,b\n1,2\n")
            deposition = _response({"id": 42, "links": {"bucket": "https://bucket"}})
            with mock.patch.object(zenodo.requests, "get", return_value=deposition) as get, \
                    mock.patch.object(zenodo.requests, "put", return_value=_response({})) as put:
                uploaded = zenodo.upload_files(42, package, "tok")
            self.assertEqual(uploaded, ["02_compilation__observations.csv", "README.md"])
            self.assertEqual(get.call_args[0][0],
                             "https://zenodo.org/api/deposit/depositions/42")
            self.assertEqual(put.call_count, 2)
            first_url = put.call_args_list[0][0][0]
            self.assertEqual(first_url, "https://bucket/02_compilation__observations.csv")

    def test_publish_hits_action_endpoint(self):
        with mock.patch.object(zenodo.requests, "post",
                               return_value=_response({"doi": "10.5281/zenodo.1"})) as post:
            result = zenodo.publish(42, "tok", sandbox=True)
        self.assertEqual(result["doi"], "10.5281/zenodo.1")
        self.assertEqual(post.call_args[0][0],
                         "https://sandbox.zenodo.org/api/deposit/depositions/42/actions/publish")

    def test_http_error_propagates(self):
        import requests
        failed = mock.Mock()
        failed.raise_for_status.side_effect = requests.HTTPError("400")
        with mock.patch.object(zenodo.requests, "post", return_value=failed):
            with self.assertRaises(requests.HTTPError):
                zenodo.create_deposit({}, "tok")


class DryRunTest(unittest.TestCase):
    def test_dry_run_makes_no_requests(self):
        with TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "README.md").write_text("r")
            with mock.patch.object(zenodo.requests, "post") as post, \
                    mock.patch.object(zenodo.requests, "get") as get, \
                    mock.patch.object(zenodo.requests, "put") as put:
                result = zenodo.dry_run(package, {"title": "T"})
            post.assert_not_called()
            get.assert_not_called()
            put.assert_not_called()
            self.assertEqual(result["files"], [{"name": "README.md", "size": 1}])

    def test_cli_default_is_dry_run(self):
        with TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "README.md").write_text("r")
            (package / "metadata.json").write_text(json.dumps({"title": "T"}))
            with mock.patch.object(zenodo.requests, "post") as post, \
                    mock.patch.object(zenodo.requests, "get") as get, \
                    mock.patch.object(zenodo.requests, "put") as put:
                code = zenodo.main(["--package", str(package)])
            self.assertEqual(code, 0)
            post.assert_not_called()
            get.assert_not_called()
            put.assert_not_called()

    def test_cli_publish_full_flow(self):
        with TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "README.md").write_text("r")
            (package / "metadata.json").write_text(json.dumps({"title": "T"}))
            deposition = _response({"id": 7, "links": {"bucket": "https://bucket"}})
            with mock.patch.object(zenodo, "resolve_token", return_value="tok"), \
                    mock.patch.object(zenodo.requests, "post",
                                      side_effect=[deposition, _response({"doi": "10.1/x"})]) as post, \
                    mock.patch.object(zenodo.requests, "get", return_value=deposition), \
                    mock.patch.object(zenodo.requests, "put", return_value=_response({})) as put:
                code = zenodo.main(["--package", str(package), "--publish", "--sandbox"])
            self.assertEqual(code, 0)
            self.assertEqual(post.call_count, 2)   # create + publish
            self.assertEqual(put.call_count, 2)    # metadata.json + README.md
            self.assertIn("sandbox.zenodo.org", post.call_args_list[0][0][0])

    def test_cli_dry_run_overrides_publish(self):
        with TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "README.md").write_text("r")
            with mock.patch.object(zenodo.requests, "post") as post:
                code = zenodo.main(["--package", str(package), "--publish", "--dry-run"])
            self.assertEqual(code, 0)
            post.assert_not_called()


class TokenTest(unittest.TestCase):
    def test_missing_token_exits(self):
        with mock.patch.object(zenodo.settings, "ZENODO_TOKEN", ""), \
                mock.patch.object(zenodo.settings, "ZENODO_SANDBOX_TOKEN", ""), \
                mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                zenodo.resolve_token()

    def test_env_fallback_and_sandbox_override(self):
        with mock.patch.object(zenodo.settings, "ZENODO_TOKEN", ""), \
                mock.patch.object(zenodo.settings, "ZENODO_SANDBOX_TOKEN", "sbx"), \
                mock.patch.dict("os.environ", {"ZENODO_TOKEN": "env-tok"}, clear=True):
            self.assertEqual(zenodo.resolve_token(), "env-tok")
            self.assertEqual(zenodo.resolve_token(sandbox=True), "sbx")


if __name__ == "__main__":
    unittest.main()
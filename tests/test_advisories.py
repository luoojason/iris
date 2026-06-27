"""Tests for the supply-chain advisory check (pure functions + audit hook)."""

from __future__ import annotations

from iris.advisories import Advisory, Component, parse_mcp_component, scan


def test_scan_flags_a_compromised_pip_version():
    adv = Advisory("x-1", "pip", "evilpkg", ("1.2.3",), "critical", "bad release")
    matches = scan({"evilpkg": "1.2.3"}, [], advisories=(adv,))
    assert len(matches) == 1
    assert matches[0][0].id == "x-1" and matches[0][1] == "1.2.3"


def test_scan_is_clean_when_version_differs_or_absent():
    adv = Advisory("x-1", "pip", "evilpkg", ("1.2.3",), "critical", "bad")
    assert scan({"evilpkg": "1.2.4"}, [], advisories=(adv,)) == []
    assert scan({}, [], advisories=(adv,)) == []


def test_scan_flags_a_pinned_compromised_npx_mcp_server():
    adv = Advisory("npm-1", "npm", "@ctrl/tinycolor", ("4.1.1",), "critical", "worm")
    comp = parse_mcp_component("npx", ["-y", "@ctrl/tinycolor@4.1.1"])
    assert comp == Component("npm", "@ctrl/tinycolor", "4.1.1")
    assert len(scan({}, [comp], advisories=(adv,))) == 1


def test_parse_mcp_component_variants():
    assert parse_mcp_component("npx", ["-y", "some-pkg@2.0.0"]) == Component("npm", "some-pkg", "2.0.0")
    assert parse_mcp_component("npx", ["plain-pkg"]) == Component("npm", "plain-pkg", None)
    assert parse_mcp_component("npx", ["-y", "@scope/name@1.0.0"]) == Component("npm", "@scope/name", "1.0.0")
    assert parse_mcp_component("npx", ["@scope/name"]) == Component("npm", "@scope/name", None)
    assert parse_mcp_component("uvx", ["tool==1.4.0"]) == Component("pip", "tool", "1.4.0")
    assert parse_mcp_component("uvx", ["tool"]) == Component("pip", "tool", None)
    assert parse_mcp_component("python", ["-m", "server"]) is None  # not npx/uvx


def test_scan_ignores_unpinned_component():
    adv = Advisory("npm-1", "npm", "p", ("1.0.0",), "critical", "x")
    # an unpinned `npx p` pulls latest; we cannot assert it is the bad version, so stay silent
    assert scan({}, [Component("npm", "p", None)], advisories=(adv,)) == []


def test_audit_flags_a_compromised_dependency(tmp_path, monkeypatch):
    import iris.advisories as adv
    from iris.audit import check_supply_chain
    from iris.config import Config

    monkeypatch.setattr(adv, "installed_pip_versions", lambda: {"evilpkg": "9.9.9"})
    monkeypatch.setattr(adv, "ADVISORIES",
                        (adv.Advisory("e", "pip", "evilpkg", ("9.9.9",), "critical", "bad"),))
    findings = check_supply_chain(Config(connections_file=str(tmp_path / "none.json")))
    assert any(f.code == "supply-chain" and f.severity == "critical" for f in findings)


def test_audit_supply_chain_silent_when_clean(tmp_path, monkeypatch):
    import iris.advisories as adv
    from iris.audit import check_supply_chain
    from iris.config import Config

    monkeypatch.setattr(adv, "installed_pip_versions", lambda: {"safe": "1.0.0"})
    monkeypatch.setattr(adv, "ADVISORIES",
                        (adv.Advisory("e", "pip", "evilpkg", ("9.9.9",), "critical", "bad"),))
    assert check_supply_chain(Config(connections_file=str(tmp_path / "none.json"))) == []

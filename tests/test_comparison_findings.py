import unittest
from pathlib import Path

from gitmaster_flash import (
    _remote_root,
    diff_status,
)


def _repo(
    rel,
    branch="main",
    remotes=(),
    modified=0,
    untracked=0,
    error="",
    remote_state="ok",
    fetch_error=False,
):
    # remotes: (name, ahead, behind) oder (name, ahead, behind, is_sync)
    return {
        "rel": rel,
        "branch": branch,
        "modified": modified,
        "untracked": untracked,
        "deleted": 0,
        "error": error,
        "remote_state": remote_state,
        "fetch_error": fetch_error,
        "remotes": [
            {
                "name": r[0],
                "ahead": r[1],
                "behind": r[2],
                "sync": bool(r[3]) if len(r) > 3 else False,
            }
            for r in remotes
        ],
    }


def _side(version="9.9.9", repos=()):
    return {"version": version, "root": "/x", "repos": list(repos)}


class DiffTests(unittest.TestCase):
    """--diff compares two machines. The split is the point: DRIFT (should be
    identical, isn't -> actionable) vs local (different branch, dirty -> explainable).
    A report that lists everything gets ignored."""

    def test_identical_means_no_output(self):
        s = [_repo("a", remotes=[("origin", 0, 0)])]
        self.assertEqual(diff_status(_side(repos=s), _side(repos=s), "here", "there"), [])

    def test_duplicate_normalized_repo_names_are_reported_as_ambiguous(self):
        """Zwei Pfade duerfen sich nach NFC-Normalisierung nicht ueberschreiben."""
        nfd, nfc = "cafe\u0301", "caf\u00e9"
        left = [_repo(nfc, branch="main"), _repo(nfc, branch="feature")]
        left[0]["path"] = f"/links/{nfd}"
        left[1]["path"] = f"/links/{nfc}"
        right = [_repo(nfc, branch="main")]

        out = diff_status(_side(repos=left), _side(repos=right), "here", "there")

        self.assertEqual(len(out), 1, out)
        self.assertIn("DRIFT", out[0])
        self.assertIn(nfc, out[0])
        self.assertIn("ambiguous", out[0])

    def test_missing_remote_is_drift(self):
        """The core case: git never transfers remotes, so they drift silently."""
        a = _side(repos=[_repo("x", remotes=[("origin", 0, 0), ("github", 0, 0)])])
        b = _side(repos=[_repo("x", remotes=[("origin", 0, 0)])])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("DRIFT", out[0])
        self.assertIn("github", out[0])
        self.assertIn("here", out[0])

    def test_missing_remote_other_direction(self):
        a = _side(repos=[_repo("x", remotes=[("origin", 0, 0)])])
        b = _side(repos=[_repo("x", remotes=[("origin", 0, 0), ("github", 0, 0)])])
        out = diff_status(a, b, "here", "there")
        self.assertIn("there", out[0])

    def test_fetch_refspec_safety_is_part_of_remote_drift(self):
        for field in (
            "fetch_refspecs_safe",
            "branch_mapping_safe",
            "fetch_url_safe",
            "push_url_safe",
        ):
            with self.subTest(field=field):
                left_repo = _repo("x", remotes=[("origin", 0, 0)])
                right_repo = _repo("x", remotes=[("origin", 0, 0)])
                left_repo["remotes"][0][field] = True
                right_repo["remotes"][0][field] = False

                out = diff_status(
                    _side(repos=[left_repo]), _side(repos=[right_repo]), "here", "there"
                )

                self.assertEqual(len(out), 1, out)
                self.assertIn("DRIFT", out[0])
                self.assertIn("origin", out[0])

    def test_differing_remote_state_is_drift(self):
        a = _side(repos=[_repo("x", remotes=[("github", 4, 2)])])
        b = _side(repos=[_repo("x", remotes=[("github", 0, 0)])])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("DRIFT", out[0])
        # Die eigene Maschine steht ohne Praeposition da ("here", nicht "on here").
        self.assertNotIn("on here", out[0])
        self.assertIn("on there", out[0])

    def test_sync_remote_equally_behind_is_shown(self):
        """Beide Rechner gleichauf, aber gemeinsam hinter dem Sync-Remote: das ist
        im reinen Zwei-Rechner-Vergleich unsichtbar, aber genau die Zahl, die
        interessiert (wie weit hinter dem Hub?). -> eigene SYNC-Zeile."""
        s = [_repo("x", remotes=[("origin", 0, 2, True)])]
        out = diff_status(_side(repos=s), _side(repos=s), "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("SYNC", out[0])
        self.assertNotIn("DRIFT", out[0])

    def test_sync_remote_in_sync_stays_silent(self):
        s = [_repo("x", remotes=[("origin", 0, 0, True)])]
        self.assertEqual(diff_status(_side(repos=s), _side(repos=s), "here", "there"), [])

    def test_offline_side_does_not_fake_drift(self):
        # fetch_failed haengt am Netz des jeweiligen Rechners und darf den Vergleich
        # nicht beeinflussen — sonst meldete jede Offline-Seite lauter Unterschiede.
        a = _side(repos=[_repo("x", remotes=[("github", 0, 0)])])
        b = _side(repos=[_repo("x", remotes=[("github", 0, 0)])])
        a["repos"][0]["remotes"][0]["fetch_failed"] = True
        self.assertEqual(diff_status(a, b, "here", "there"), [])

    def test_nonsync_remote_equally_behind_stays_silent(self):
        # Fuer Nicht-Sync-Remotes (z.B. github) bleibt gleicher Stand = kein Report.
        s = [_repo("x", remotes=[("github", 0, 2)])]
        self.assertEqual(diff_status(_side(repos=s), _side(repos=s), "here", "there"), [])

    def test_sync_remote_reported_before_other_remotes(self):
        a = _side(repos=[_repo("x", remotes=[("github", 4, 0), ("origin", 1, 0, True)])])
        b = _side(repos=[_repo("x", remotes=[("github", 0, 0), ("origin", 0, 0, True)])])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 2)
        self.assertIn("origin", out[0])  # Sync-Remote zuerst, github danach
        self.assertIn("github", out[1])

    def test_failed_fetch_is_not_drift(self):
        """Der Vorfall vom 2026-08-05: `gmf --diff` laeuft auf der Gegenseite per
        ssh, dort kommt der Credential-Helper nicht an den Schluesselbund, und jedes
        GitHub-Remote scheitert. Das erschien als zwei DRIFT-Zeilen (`error`,
        `remote_state`) je Repo und las sich wie ein kaputter Login auf dem anderen
        Mac. Es ist aber ein Merkmal der messenden Sitzung, kein Unterschied."""
        a = _side(repos=[_repo("x"), _repo("y")])
        b = _side(
            repos=[
                _repo(
                    "x",
                    error="github: Schlüsselbund unerreichbar",
                    remote_state="error",
                    fetch_error=True,
                ),
                _repo(
                    "y",
                    error="github: Schlüsselbund unerreichbar",
                    remote_state="error",
                    fetch_error=True,
                ),
            ]
        )
        out = diff_status(a, b, "here", "there")
        self.assertEqual([line for line in out if "DRIFT" in line], [])
        # Verschwiegen wird es aber nicht: EINE Zeile nennt Seite und Anzahl.
        erklaerung = [line for line in out if "there" in line]
        self.assertEqual(len(erklaerung), 1, out)
        self.assertIn("2", erklaerung[0])

    def test_failed_fetch_still_compares_conflicts_and_stashes(self):
        """Nur `error` und `remote_state` haengen am Fetch. Ein Merge-Konflikt oder
        ein Stash auf genau einer Seite bleibt ein echter Unterschied — sonst wuerde
        ein gescheiterter Fetch echte Befunde mitverschlucken."""
        a = _side(repos=[_repo("x")])
        b = _side(repos=[_repo("x", remote_state="error", fetch_error=True)])
        b["repos"][0]["conflicts"] = 2
        a["repos"][0]["conflicts"] = 0
        out = diff_status(a, b, "here", "there")
        drift = [line for line in out if "DRIFT" in line]
        self.assertEqual(len(drift), 1, out)
        self.assertIn("conflicts", drift[0])

    def test_error_without_fetch_error_stays_drift(self):
        """Gegenprobe: Ein Fehler, der NICHT vom Fetch kommt (kaputtes Repo, lokaler
        Lesefehler), muss weiter als DRIFT erscheinen."""
        a = _side(repos=[_repo("x")])
        b = _side(repos=[_repo("x", error="kaputt", remote_state="error")])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len([line for line in out if "DRIFT" in line]), 2, out)

    def test_a_stale_tracking_ref_after_a_failed_fetch_is_no_drift(self):
        """Der Fetch dieses Remotes scheiterte auf einer Seite.

        Dort steht der Tracking-Ref vom letzten gelungenen Lauf, drueben der
        frische — und schon meldete gmf eine DRIFT-Zeile mit zwei
        Ahead/Behind-Paaren, direkt neben dem Satz, der Stand sei gar nicht
        messbar. Genau dieser Widerspruch war der Fehlalarm."""
        a = _side(
            repos=[
                _repo(
                    "x",
                    remotes=[("origin", 0, 7, True)],
                    error="origin: Schlüsselbund unerreichbar",
                    remote_state="error",
                    fetch_error=True,
                )
            ]
        )
        b = _side(repos=[_repo("x", remotes=[("origin", 0, 0, True)])])
        a["repos"][0]["remotes"][0]["fetch_failed"] = True
        out = diff_status(a, b, "here", "there")
        self.assertEqual([line for line in out if "DRIFT" in line or "SYNC" in line], [], out)
        # Nur die eine Zeile, die die betroffene Seite benennt, bleibt uebrig.
        self.assertEqual(len(out), 1, out)

    def test_a_local_error_survives_a_failed_fetch_on_the_other_side(self):
        """Kreuzfall: hier ein gescheiterter Fetch, drueben ein echter lokaler
        Schaden (unlesbarer Index). Vorher blendete der Fetch-Fehler das Feld
        `error` auf BEIDEN Seiten aus — das Fetch-Problem hier versteckte den
        Repo-Schaden dort."""
        a = _side(
            repos=[_repo("x", error="origin: kein Netz", remote_state="error", fetch_error=True)]
        )
        b = _side(repos=[_repo("x", error="cannot read Git index")])
        out = diff_status(a, b, "here", "there")
        drift = [line for line in out if "DRIFT" in line]
        self.assertEqual(len(drift), 1, out)
        self.assertIn("error", drift[0])
        self.assertIn("cannot read Git index", drift[0])
        # Der fetchbedingte Fehler DIESER Seite bleibt trotzdem draussen.
        self.assertNotIn("kein Netz", drift[0])

    def test_different_branch_is_local_not_drift(self):
        a = _side(repos=[_repo("x", branch="main")])
        b = _side(repos=[_repo("x", branch="feature")])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertNotIn("DRIFT", out[0])

    def test_dirty_is_local_not_drift(self):
        a = _side(repos=[_repo("x", modified=2, untracked=1)])
        b = _side(repos=[_repo("x")])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertNotIn("DRIFT", out[0])
        self.assertIn("3", out[0])

    def test_repo_only_on_one_side(self):
        a = _side(repos=[_repo("here-only"), _repo("both")])
        b = _side(repos=[_repo("both")])
        out = diff_status(a, b, "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("here-only", out[0])

    def test_version_mismatch_is_flagged_first(self):
        out = diff_status(_side("1.0.0"), _side("2.0.0"), "here", "there")
        self.assertTrue(out[0].startswith("!"))

    def test_errors_conflicts_stashes_and_remote_state_are_compared(self):
        broken = _repo("x")
        broken.update(error="git failed", conflicts=2, stashes=1, remote_state="error")
        clean = _repo("x")
        clean.update(error="", conflicts=0, stashes=0, remote_state="ok")
        out = diff_status(_side(repos=[broken]), _side(repos=[clean]), "here", "there")
        joined = "\n".join(out)
        for field in ("error", "conflicts", "stashes", "remote_state"):
            self.assertIn(field, joined)

    def test_remote_endpoint_fingerprint_drift_is_compared(self):
        a = _repo("x", remotes=[("origin", 0, 0, True)])
        b = _repo("x", remotes=[("origin", 0, 0, True)])
        a["remotes"][0]["fetch_fingerprint"] = "aaa"
        b["remotes"][0]["fetch_fingerprint"] = "bbb"
        out = diff_status(_side(repos=[a]), _side(repos=[b]), "here", "there")
        self.assertTrue(any("security/endpoint" in line for line in out))

    def test_all_fetch_fingerprints_are_compared(self):
        a = _repo("x", remotes=[("origin", 0, 0, True)])
        b = _repo("x", remotes=[("origin", 0, 0, True)])
        a["remotes"][0]["fetch_fingerprints"] = ["aaa", "common"]
        b["remotes"][0]["fetch_fingerprints"] = ["bbb", "common"]
        out = diff_status(_side(repos=[a]), _side(repos=[b]), "here", "there")
        self.assertTrue(any("security/endpoint" in line for line in out))

    def test_branch_presence_on_same_branch_is_state_drift_not_security(self):
        """Gleicher Branch, aber nur eine Seite kennt ihn auf dem Remote: das ist
        ein Zustandsunterschied — keine Sicherheits-/Endpunkt-Meldung."""
        a = _repo("x", remotes=[("origin", 0, 0)])
        b = _repo("x", remotes=[("origin", 0, 0)])
        a["remotes"][0]["branch_exists"] = True
        b["remotes"][0]["branch_exists"] = False
        out = diff_status(_side(repos=[a]), _side(repos=[b]), "here", "there")
        self.assertEqual(len(out), 1)
        self.assertIn("DRIFT", out[0])
        self.assertIn("main", out[0])
        self.assertIn("here", out[0])
        self.assertNotIn("security/endpoint", out[0])

    def test_branch_presence_with_different_branches_is_no_drift(self):
        """Zwei Rechner mit identischen Remotes, aber verschiedenen Branches:
        `branch_exists` hängt am ausgecheckten Branch und darf keine
        Sicherheits-Drift und keinen Exit 1 wegen Endpunkt-Identität erzeugen."""
        a = _repo("x", branch="main", remotes=[("origin", 0, 0)])
        b = _repo("x", branch="feature", remotes=[("origin", 0, 0)])
        a["remotes"][0]["branch_exists"] = True
        b["remotes"][0]["branch_exists"] = False
        out = diff_status(_side(repos=[a]), _side(repos=[b]), "here", "there")
        self.assertEqual(len(out), 1)  # nur die erklärbare local-Zeile
        self.assertNotIn("DRIFT", out[0])

    def test_remote_root_uses_remote_home(self):
        """Home dirs differ between machines (/Users/anna vs /home/bob) — the path
        must be resolved against the REMOTE $HOME, not pasted absolutely."""
        r = _remote_root(Path.home() / "git", None)
        self.assertEqual(r, "~/git")
        self.assertIn("git", r)
        self.assertNotIn(str(Path.home()), r)

    def test_remote_root_explicit_path_wins(self):
        self.assertEqual(_remote_root(Path.home() / "git", "/srv/code"), "/srv/code")

    def test_remote_root_outside_home_stays_absolute(self):
        self.assertEqual(_remote_root(Path("/srv/code"), None), "/srv/code")

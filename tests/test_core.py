import json

from conftest import call, make_hermes_db, result, user
from mnemobius import bridge, crystallize, distill, forge, lens, resonate, store, textkit, turn, vigil, witness
from mnemobius.redact import has_secret, is_sensitive, redact


# ---- redaction & privacy ----------------------------------------------------------------------
def test_redact_secrets_and_pii():
    s = redact("key sk-" + "a" * 30 + " mail bob@example.com ip 10.0.0.5 path /home/bob/x token ghp_" + "B" * 30)
    assert "sk-" not in s and "bob@" not in s and "10.0.0.5" not in s and "/home/bob" not in s and "ghp_" not in s
    assert not has_secret(s)


def test_long_filenames_are_not_secrets():
    assert redact("not found: /x/a-very-long-descriptive-file-name-for-a-skill-reference-document-v2.md").endswith("v2.md")


def test_sensitive_topics_detected():
    assert is_sensitive("she has a fever and I'm unsure about the relationship")
    assert is_sensitive("tell Zed about it", extra=["zed"])
    assert not is_sensitive("fix the failing build")


def test_sensitive_ask_is_stored_without_text():
    atoms = distill.distill_session([user("my girlfriend and I broke up, what now"), user("fix the build please")])
    asks = [a for a in atoms if a["kind"] == "ask"]
    assert asks[0]["key"] == "sensitive" and asks[0]["text"] == ""
    assert "build" in asks[1]["text"].split() and "fix the build" not in asks[1]["text"]


def test_cron_and_subagent_prompts_are_never_corrections():
    msg = [user("Do NOT merge. Never touch main. Stop if tests fail.")]
    assert not [a for a in distill.distill_session(msg, human=False) if a["kind"] == "correction"]
    assert [a for a in distill.distill_session(msg, human=True) if a["kind"] == "correction"]


def test_notes_ingest_skips_private_folders(home, tmp_path):
    d = tmp_path / "notes"
    (d / "private").mkdir(parents=True)
    (d / "private" / "x.md").write_text("Lesson: never tell anyone about the private thing.\n")
    (d / "ok.md").write_text("Lesson: always run the audit after editing a skill file.\n")
    conn = store.connect()
    st = witness.ingest_notes(conn, d)
    assert st["note_files"] == 1 and st["lessons"] == 1


# ---- distill -----------------------------------------------------------------------------------
def test_error_normalisation_is_stable():
    a = textkit.normalize_error("Error: no such file '/home/x/abc123.txt' at line 42")
    b = textkit.normalize_error("Error: no such file '/tmp/zzz999.txt' at line 7")
    assert a == b and textkit.signature("timeout after 30s") == textkit.signature("timeout after 45s")


def test_classify_result_cases():
    assert distill.classify_result("terminal", '{"output": "ok", "exit_code": 0}')[0]
    ok, msg = distill.classify_result("terminal", '{"output": "boom\\nfatal: bad", "exit_code": 1}')
    assert not ok and "fatal" in msg
    assert distill.classify_result("patch", '{"error": "could not find match"}') == (False, "could not find match")
    assert distill.classify_result("write_file", '{"bytes_written": 10, "verified": true, "lint": {"status": "error"}}')[0]


def test_distill_pairs_calls_and_results():
    msgs = [user("please fix the thing in the repo"), call("terminal", {"command": "git status"}), result("terminal", '{"exit_code": 0, "output": "x"}')]
    kinds = [a["kind"] for a in distill.distill_session(msgs)]
    assert kinds == ["ask", "call", "ok"]


# ---- recovery mining & the full cycle ----------------------------------------------------------
def build_sessions(n=6, recover=True):
    out = []
    for i in range(n):
        msgs = [user(f"deploy the staging site number {i} and check the health endpoint please"),
                call("terminal", {"command": "curl https://x/health"}, "a"), result("terminal", '{"output": "connection refused", "exit_code": 7}', "a"),
                call("read_file", {"path": "/p/config.yaml"}, "b"), result("read_file", '{"content": "port: 8080"}', "b"),
                call("terminal", {"command": "curl https://x:8080/health"}, "c"),
                result("terminal", '{"output": "ok", "exit_code": 0}' if recover else '{"output": "connection refused", "exit_code": 7}', "c")]
        out.append((f"s{i}", "telegram", msgs))
    return out


def run_cycle(home, tmp_path, sessions):
    db = tmp_path / "state.db"
    make_hermes_db(str(db), sessions)
    conn = store.connect()
    witness.ingest_hermes(conn, db)
    resonate.resonate(conn)
    crystallize.crystallize(conn)
    bridge.bridge(conn)
    return conn


def test_recovery_pattern_is_found_with_remedy(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions())
    p = conn.execute("SELECT * FROM concepts WHERE kind='pitfall'").fetchall()
    assert p, "a recurring failure must become a pitfall"
    d = json.loads(p[0]["data"])
    assert d["tool"] == "terminal" and d["recovered_rate"] == 1.0
    assert d["remedies"][0]["path"] == "read_file" and d["remedies"][0]["cls"] == "inspect-first"


def test_no_recovery_is_reported_honestly(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(recover=False))
    d = json.loads(conn.execute("SELECT data FROM concepts WHERE kind='pitfall'").fetchone()[0])
    assert d["recovered_rate"] == 0.0
    st = conn.execute("SELECT statement FROM concepts WHERE kind='pitfall'").fetchone()[0]
    assert "No reliable recovery" in st


def test_ingest_is_incremental(home, tmp_path):
    db = tmp_path / "state.db"
    make_hermes_db(str(db), build_sessions(3))
    conn = store.connect()
    assert witness.ingest_hermes(conn, db)["sessions_ingested"] == 3
    again = witness.ingest_hermes(conn, db)
    assert again["sessions_ingested"] == 0 and again["skipped_unchanged"] == 3


def test_witness_never_writes_the_hermes_db(home, tmp_path):
    db = tmp_path / "state.db"
    make_hermes_db(str(db), build_sessions(3))
    before = db.read_bytes()
    witness.ingest_hermes(store.connect(), db)
    assert db.read_bytes() == before


# ---- confidence ladder, decay, feedback --------------------------------------------------------
def test_tier_never_inflates_without_outcomes(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(10))
    assert not conn.execute("SELECT 1 FROM concepts WHERE tier='verified'").fetchall()


def test_outcomes_promote_to_verified_and_failures_demote(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(10))
    cid = conn.execute("SELECT id FROM concepts WHERE kind='pitfall'").fetchone()[0]
    for _ in range(3):
        turn._credit(conn, cid, True)
    conn.commit()
    assert conn.execute("SELECT tier FROM concepts WHERE id=?", (cid,)).fetchone()[0] == "verified"
    for _ in range(12):
        turn._credit(conn, cid, False)
    conn.commit()
    assert conn.execute("SELECT tier FROM concepts WHERE id=?", (cid,)).fetchone()[0] != "verified"


def test_confidence_decays_toward_doubt():
    t0 = 1_000_000.0
    fresh = crystallize.effective(0.9, t0, "pitfall", t0)
    old = crystallize.effective(0.9, t0, "pitfall", t0 + 600 * 86400)
    assert fresh > old > 0.5


def test_echo_loop_credits_the_concept_and_the_arm(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(6))
    cid = conn.execute("SELECT id FROM concepts WHERE kind='pitfall'").fetchone()[0]
    before = conn.execute("SELECT a FROM arms WHERE name='pitfall'").fetchone()[0]
    turn.record_echo(conn, "sess", "terminal", "sigx", cid)
    assert turn.settle_echo(conn, "sess", "terminal", True) == 1
    assert conn.execute("SELECT fb_ok FROM concepts WHERE id=?", (cid,)).fetchone()[0] == 1
    assert conn.execute("SELECT a FROM arms WHERE name='pitfall'").fetchone()[0] > before


def test_a_different_failure_is_inconclusive(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(6))
    cid = conn.execute("SELECT id FROM concepts WHERE kind='pitfall'").fetchone()[0]
    turn.record_echo(conn, "sess", "terminal", "sig-a", cid)
    turn.settle_echo(conn, "sess", "terminal", False, "sig-b")
    c = conn.execute("SELECT fb_ok, fb_fail FROM concepts WHERE id=?", (cid,)).fetchone()
    assert (c["fb_ok"], c["fb_fail"]) == (0, 0)


def test_ebb_retires_old_unconfirmed_and_snapshots(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(6))
    conn.execute("UPDATE concepts SET last_seen=1000000, first_seen=1000000")
    conn.commit()
    r = turn.ebb(conn)
    assert r["retired"] >= 1 and conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0] == r["retired"]


def test_inferred_outcomes_count_half_and_skip_unclear(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(6))
    cid = conn.execute("SELECT id FROM concepts WHERE kind='pitfall'").fetchone()[0]
    conn.execute("INSERT INTO briefs(id,ts,channel,task,session,concepts,strategies) VALUES('b1',1,'x','t','s0',?,'[]')", (json.dumps([cid]),))
    conn.commit()
    out = turn.infer_outcomes(conn)
    assert out["good"] + out["bad"] + out["unknown"] == 1
    if out["good"]:
        assert conn.execute("SELECT fb_ok FROM concepts WHERE id=?", (cid,)).fetchone()[0] == 0.5


# ---- lens, forge, vigil ------------------------------------------------------------------------
def test_brief_surfaces_relevant_pitfall_and_is_bounded(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(8))
    text, bid = lens.brief(conn, "deploy the staging site and curl the health endpoint", session="s")
    assert bid and "terminal" in text and len(text) <= lens.BRIEF_CHARS


def test_brief_is_empty_for_unrelated_task(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(8))
    text, _ = lens.brief(conn, "compose a sonnet about autumn leaves")
    assert text == ""


def test_forge_gives_precedent_traps_and_verification(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(8))
    plan = forge.forge(conn, "deploy the staging site and check the health endpoint")
    assert plan["precedent"] and plan["watch"] and plan["verify"]
    assert "PRECEDENT" in forge.render_plan(plan)


def test_forge_admits_when_it_has_no_precedent(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(8))
    plan = forge.forge(conn, "translate a poem into klingon")
    assert plan["precedent"] is None and "No precedent" in plan["verify"]


def test_vigil_detects_and_purges_planted_secret(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(3))
    conn.execute("INSERT INTO atoms(ep,seq,ts,kind,key,sig,text) VALUES('s0',999,1,'ask','','','my key is sk-" + "z" * 30 + "')")
    conn.commit()
    assert not vigil.audit(conn)["clean"]
    vigil.purge(conn)
    assert vigil.audit(conn)["clean"]


def test_database_is_private(home):
    store.connect()
    assert (home / "core.db").stat().st_mode & 0o077 == 0


def test_topic_words_drop_urls_and_identifiers():
    from mnemobius.textkit import topic_words
    out = topic_words(["http", "com", "deploy", "q9x2k7m4w1z8c5v3b6n0", "long-descriptive-report-name-for-a-server-assessment-2026", "staging"])
    assert out == ["deploy", "staging"]


def test_concepts_carry_no_raw_request_text(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(8))
    for r in conn.execute("SELECT statement, data FROM concepts WHERE kind IN ('archetype','preference')"):
        assert "deploy the staging site number" not in r["statement"] + (r["data"] or "")


def test_asks_are_stored_as_unordered_word_bags_never_sentences():
    atoms = distill.distill_session([user("Please rewrite the onboarding checklist for the new tenant before Friday")])
    text = [a for a in atoms if a["kind"] == "ask"][0]["text"]
    assert "onboard" in text.split() and "checklist" in text.split()
    assert text == " ".join(sorted(set(text.split())))  # sorted + unique: word order is gone
    assert "before Friday" not in text


def test_quoted_assistant_replies_are_not_the_humans_words():
    assert distill.clean_user('[Replying to: "I hear you, Sir. That is a lot to carry."] can you fix the build') == "can you fix the build"
    assert distill.clean_user('[Replying to: "only a quote"]') is None


def test_error_text_that_echoes_private_content_is_blanked():
    msgs = [call("skill_manage", {"action": "patch"}), result("skill_manage", '{"error": "no entry matched therapy: private journal notes"}')]
    fail = [a for a in distill.distill_session(msgs) if a["kind"] == "fail"][0]
    assert fail["text"] == "" and fail["sig"]
    msgs = [call("terminal", {"command": "sudo x"}), result("terminal", '{"output": "sudo: a password is required", "exit_code": 1}')]
    assert [a for a in distill.distill_session(msgs) if a["kind"] == "fail"][0]["text"]  # credentials words are fine in tool errors


def test_vigil_retroactively_cleans_text_stored_under_an_older_filter(home, tmp_path):
    conn = run_cycle(home, tmp_path, build_sessions(3))
    conn.execute("INSERT INTO atoms(ep,seq,ts,kind,key,sig,text) VALUES('s0',998,1,'ask','','','my therapist said take it slowly')")
    conn.execute("INSERT INTO briefs(id,ts,channel,task,session,concepts,strategies) VALUES('bx',1,'x','my family and my debt','s0','[]','[]')")
    conn.commit()
    assert vigil.audit(conn)["by_kind"].get("sensitive", 0) == 2
    vigil.purge(conn)
    assert vigil.audit(conn)["clean"]
    assert conn.execute("SELECT COUNT(*) FROM atoms WHERE seq=998").fetchone()[0] == 1  # the row stays; only the words go


def test_briefs_store_topic_words_not_the_task(home, tmp_path):
    from mnemobius import lens
    conn = run_cycle(home, tmp_path, build_sessions(8))
    text, bid = lens.brief(conn, "deploy the staging site and curl the health endpoint please, my mum asked", session="s0")
    stored = conn.execute("SELECT task FROM briefs WHERE id=?", (bid,)).fetchone()[0]
    assert "deploy" in stored and "my mum asked" not in stored and "," not in stored


def test_websites_visited_are_never_recorded_and_sensitive_skill_names_are_skipped():
    msgs = [call("web_extract", {"urls": ["https://example.org/docs"]}), result("web_extract", '{"content": "x"}'),
            call("skill_view", {"name": "therapy-notes"}, "c2"), result("skill_view", '{"ok": true}', "c2"),
            call("skill_view", {"name": "deploy-checks"}, "c3"), result("skill_view", '{"ok": true}', "c3")]
    ents = [a["key"] for a in distill.distill_session(msgs) if a["kind"] == "entity"]
    assert ents == ["skill:deploy-checks"]


def test_argument_digests_never_carry_skill_or_script_names():
    assert "therapy" not in distill.arg_digest("skill_view", {"name": "team-therapy-notes"})
    assert distill.arg_digest("skill_view", {"name": "a"}) == distill.arg_digest("skill_view", {"name": "a"})
    assert distill.arg_digest("skill_view", {"name": "a"}) != distill.arg_digest("skill_view", {"name": "b"})
    d = distill.arg_digest("terminal", {"command": "python3 /x/acme-private-tool.py --go"})
    assert "acme" not in d and d.startswith("python #")
    assert distill.arg_digest("terminal", {"command": "git commit -m x"}) == "git commit"


def test_unknown_first_words_and_hex_blobs_are_hashed_in_digests():
    d = distill.arg_digest("terminal", {"command": "./personal-notes-2026-07-22 --run"})
    assert "personal" not in d and d.startswith("cmd #")
    assert distill.arg_digest("terminal", {"command": "01c79a2385f47ef46ea93622db49ce3bd0595ab16a1fba13 x"}).startswith("cmd #")
    assert distill.arg_digest("terminal", {"command": "cd /x && git status"}) == "git status"
    assert distill.arg_digest("terminal", {"command": "ls -la"}) == "ls"

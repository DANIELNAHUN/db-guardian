from db_guardian.clones.diff import diff_schema, normalize_create_sql, version_warnings

A = "CREATE TABLE `a` (\n  `id` int NOT NULL\n) ENGINE=InnoDB"
B = "CREATE TABLE `b` (\n  `id` int NOT NULL\n) ENGINE=InnoDB"
C = "CREATE TABLE `c` (\n  `id` int NOT NULL,\n  `name` varchar(100)\n) ENGINE=InnoDB"
C_CHANGED = "CREATE TABLE `c` (\n  `id` int NOT NULL,\n  `full_name` varchar(50)\n) ENGINE=InnoDB"
Z = "CREATE TABLE `z` (\n  `id` int NOT NULL\n) ENGINE=InnoDB"
UTF8_0900_SQL = "CREATE TABLE `t` (`id` int) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"


def test_auto_increment_ignored():
    a = "CREATE TABLE `t` (\n  `id` int NOT NULL AUTO_INCREMENT\n) ENGINE=InnoDB AUTO_INCREMENT=51 DEFAULT CHARSET=utf8mb4"
    b = a.replace("AUTO_INCREMENT=51", "AUTO_INCREMENT=7")
    assert normalize_create_sql(a) == normalize_create_sql(b)
    assert "NOT NULL AUTO_INCREMENT" in normalize_create_sql(a)


def test_whitespace_collapsed():
    assert normalize_create_sql("CREATE  TABLE `t`\n  (`id` int)") == "CREATE TABLE `t` (`id` int)"


def test_diff_categories():
    d = diff_schema({"a": A, "b": B, "c": C}, {"b": B, "c": C_CHANGED, "z": Z})
    assert d.missing_in_target == ["a"]
    assert d.structure_differs == ["c"]
    assert d.only_in_target == ["z"]
    assert d.matching == ["b"]


def test_diff_ignores_auto_increment_counter():
    with_counter = B.replace("ENGINE=InnoDB", "ENGINE=InnoDB AUTO_INCREMENT=99")
    assert diff_schema({"b": B}, {"b": with_counter}).matching == ["b"]


def test_same_versions_no_warnings():
    assert version_warnings("8.0.46", "8.0.46", [UTF8_0900_SQL]) == []


def test_newer_target_no_warnings():
    assert version_warnings("8.0.46", "8.4.3", [UTF8_0900_SQL]) == []


def test_older_target_warns():
    assert any("older" in w for w in version_warnings("8.0.46", "5.7.41", []))


def test_0900_collation_into_57_warns():
    assert any("utf8mb4_0900_ai_ci" in w for w in version_warnings("8.0.46", "5.7.41", [UTF8_0900_SQL]))


def test_0900_collation_into_mariadb_warns():
    warnings = version_warnings("8.0.46", "10.11.6-MariaDB", [UTF8_0900_SQL])
    assert any("utf8mb4_0900_ai_ci" in w for w in warnings)


def test_unknown_source_version_skips_comparison():
    assert version_warnings(None, "8.0.46", []) == []

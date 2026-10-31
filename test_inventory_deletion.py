from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

import inventory_deletion
from inventory_service import InventoryService
from storage import Storage


OWNER, MEMBER, OUTSIDER = 101, 202, 303


@pytest.fixture
def inventory(tmp_path):
    storage = Storage(tmp_path / "synthetic-inventory.sqlite3")
    for actor in (OWNER, MEMBER, OUTSIDER):
        storage.record_user(actor, "synthetic", "Synthetic")
    home = storage.create_household(OWNER, "Synthetic shared home")
    storage.join_household(MEMBER, home.invite_code)
    stores = storage.list_stores(home.id)
    active = storage.add_or_update_item(stores[0].id, OWNER, "Synthetic rice", 2, "кг", 1)
    archived = storage.add_or_update_item(stores[0].id, OWNER, "Synthetic milk", 1, "л", 2)
    storage.archive_item(stores[0].id, OWNER, archived.id)
    retained = storage.add_or_update_item(stores[1].id, OWNER, "Synthetic oats", 3, "кг", 1)
    owner_fallback = storage.create_household(OWNER, "Synthetic owner fallback")
    member_fallback = storage.create_household(MEMBER, "Synthetic member fallback")
    foreign = storage.create_household(OUTSIDER, "Synthetic foreign home")
    foreign_store = storage.list_stores(foreign.id)[0]
    foreign_item = storage.add_or_update_item(foreign_store.id, OUTSIDER, "Synthetic foreign stock", 4, "шт", 1)
    return {
        "storage": storage, "home": home, "stores": stores, "active": active, "archived": archived,
        "retained": retained, "owner_fallback": owner_fallback, "member_fallback": member_fallback,
        "foreign": foreign, "foreign_store": foreign_store, "foreign_item": foreign_item,
    }


def product_state(storage):
    with closing(storage._connect()) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'deletion_previews' ORDER BY name")]
        return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')] for table in tables}


def foreign_state(inventory):
    storage, home = inventory["storage"], inventory["foreign"]
    with closing(storage._connect()) as connection:
        return {
            "household": tuple(connection.execute("SELECT * FROM households WHERE id = ?", (home.id,)).fetchone()),
            "members": [tuple(row) for row in connection.execute("SELECT * FROM household_members WHERE household_id = ?", (home.id,))],
            "stores": [tuple(row) for row in connection.execute("SELECT * FROM stores WHERE household_id = ?", (home.id,))],
            "items": [tuple(row) for row in connection.execute("SELECT * FROM inventory_items WHERE household_id = ?", (home.id,))],
            "events": [tuple(row) for row in connection.execute("SELECT e.* FROM item_events e JOIN inventory_items i ON i.id = e.item_id WHERE i.household_id = ?", (home.id,))],
            "user": tuple(connection.execute("SELECT * FROM users WHERE chat_id = ?", (OUTSIDER,)).fetchone()),
        }


def token_exists(storage, token):
    with closing(storage._connect()) as connection:
        return connection.execute("SELECT 1 FROM deletion_previews WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone() is not None


def assert_foreign_keys(storage):
    with closing(storage._connect()) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def target_id(inventory, target_type):
    return {"item": inventory["archived"].id, "store": inventory["stores"][0].id, "household": inventory["home"].id}[target_type]


@pytest.mark.parametrize("target_type", ["item", "store", "household"])
def test_preview_is_private_and_does_not_modify_product_state(inventory, target_type):
    storage = inventory["storage"]
    before = product_state(storage)
    preview = storage.preview_deletion(OWNER, target_type, target_id(inventory, target_type))
    assert product_state(storage) == before
    assert set(preview) == {"preview_token", "target_type", "target_id", "name", "household_id", "household_name", "stores", "active_items", "archived_items", "events", "members", "shared", "expires_at"}
    assert preview["members"] == 2 and preview["shared"] is True
    assert preview["household_id"] == inventory["home"].id
    assert all(set(store) == {"id", "name"} for store in preview["stores"])
    assert preview["preview_token"].encode() not in storage.path.read_bytes()
    assert inventory_deletion.PREVIEW_TTL - 1 <= preview["expires_at"] - int(inventory_deletion.time.time()) <= inventory_deletion.PREVIEW_TTL
    assert token_exists(storage, preview["preview_token"])


@pytest.mark.parametrize("target_type,counts", [
    ("item", {"stores": 0, "active_items": 0, "archived_items": 1, "events": 2, "members": 0}),
    ("store", {"stores": 1, "active_items": 1, "archived_items": 1, "events": 3, "members": 0}),
    ("household", {"stores": 3, "active_items": 2, "archived_items": 1, "events": 4, "members": 2}),
])
def test_reviewed_delete_cascades_only_target_descendants(inventory, target_type, counts):
    storage = inventory["storage"]
    original_foreign = foreign_state(inventory)
    identity = target_id(inventory, target_type)
    preview = storage.preview_deletion(OWNER, target_type, identity)
    receipt = storage.delete_reviewed(OWNER, target_type, identity, preview["preview_token"])
    assert receipt == {
        "target_type": target_type, "target_id": identity, "name": preview["name"],
        "household_id": inventory["home"].id, "deleted": True, **counts,
    }
    assert not token_exists(storage, preview["preview_token"])
    assert storage.get_item(inventory["archived"].id) is None
    if target_type == "item":
        assert storage.get_item(inventory["active"].id) is not None
        assert storage.get_store(inventory["stores"][0].id) is not None
    elif target_type == "store":
        assert storage.get_item(inventory["active"].id) is None
        assert storage.get_store(inventory["stores"][0].id) is None
        assert storage.get_item(inventory["retained"].id) is not None
    else:
        assert all(home.id != identity for home in storage.list_households(OWNER))
        assert storage.get_item(inventory["retained"].id) is None
    assert foreign_state(inventory) == original_foreign
    assert_foreign_keys(storage)
    before_replay = product_state(storage)
    with pytest.raises(ValueError):
        storage.delete_reviewed(OWNER, target_type, identity, preview["preview_token"])
    assert product_state(storage) == before_replay


@pytest.mark.parametrize("target_type", ["item", "store"])
def test_member_can_review_and_delete_stock_or_store(inventory, target_type):
    service = InventoryService(inventory["storage"], MEMBER)
    identity = target_id(inventory, target_type)
    preview = service.preview_deletion(target_type, identity)
    receipt = getattr(service, "delete_" + target_type)(identity, preview["preview_token"], confirmed=True)
    assert receipt["deleted"] is True
    assert_foreign_keys(inventory["storage"])


def test_household_delete_is_owner_only_and_never_becomes_member_leave(inventory):
    storage, home = inventory["storage"], inventory["home"]
    before = product_state(storage)
    with pytest.raises(ValueError, match="недоступен"):
        storage.preview_deletion(MEMBER, "household", home.id)
    preview = storage.preview_deletion(OWNER, "household", home.id)
    with pytest.raises(ValueError):
        storage.delete_reviewed(MEMBER, "household", home.id, preview["preview_token"])
    assert product_state(storage) == before
    assert storage.is_household_member(home.id, MEMBER)
    # The existing interactive member-leave API remains a separate operation.
    assert storage.remove_household_for_user(MEMBER, home.id) == "left"
    assert any(candidate.id == home.id for candidate in storage.list_households(OWNER))


@pytest.mark.parametrize("target_type", ["item", "store", "household"])
def test_nonmember_and_unknown_target_fail_generically_without_changes(inventory, target_type):
    storage = inventory["storage"]
    before = product_state(storage)
    errors = []
    for identity in (target_id(inventory, target_type), 999999):
        with pytest.raises(ValueError) as error:
            storage.preview_deletion(OUTSIDER, target_type, identity)
        errors.append(str(error.value))
    assert errors == ["Объект не найден или недоступен."] * 2
    assert product_state(storage) == before


@pytest.mark.parametrize("target_type", ["item", "store", "household"])
def test_permissions_are_checked_again_after_preview(inventory, target_type):
    storage = inventory["storage"]
    identity = target_id(inventory, target_type)
    preview = storage.preview_deletion(OWNER, target_type, identity)
    with closing(storage._connect()) as connection, connection:
        connection.execute("DELETE FROM household_members WHERE household_id = ? AND chat_id = ?", (inventory["home"].id, OWNER))
    before = product_state(storage)
    with pytest.raises(ValueError, match="недоступен"):
        storage.delete_reviewed(OWNER, target_type, identity, preview["preview_token"])
    assert product_state(storage) == before
    assert token_exists(storage, preview["preview_token"])


@pytest.mark.parametrize("confirmed", [False, None, 0, 1, "true"])
def test_exact_confirmation_is_required_without_consuming_preview(inventory, confirmed):
    storage = inventory["storage"]
    service = InventoryService(storage, OWNER)
    identity = inventory["active"].id
    preview = service.preview_deletion("item", identity)
    before = product_state(storage)
    with pytest.raises(ValueError, match="Подтвердите"):
        service.delete_item(identity, preview["preview_token"], confirmed=confirmed)
    assert product_state(storage) == before and token_exists(storage, preview["preview_token"])
    assert service.delete_item(identity, preview["preview_token"], confirmed=True)["deleted"]


def test_wrong_actor_type_target_and_token_do_not_consume_valid_preview(inventory):
    storage = inventory["storage"]
    identity = inventory["stores"][0].id
    preview = storage.preview_deletion(OWNER, "store", identity)
    token = preview["preview_token"]
    before = product_state(storage)
    for actor, kind, target, supplied in (
        (MEMBER, "store", identity, token),
        (OWNER, "item", inventory["active"].id, token),
        (OWNER, "store", inventory["stores"][1].id, token),
        (OWNER, "store", identity, "x" * 43),
        (OWNER, "store", identity, "invalid"),
        (OWNER, "unsupported", identity, token),
        (OWNER, "store", True, token),
    ):
        with pytest.raises(ValueError):
            storage.delete_reviewed(actor, kind, target, supplied)
        assert token_exists(storage, token) and product_state(storage) == before
    assert storage.delete_reviewed(OWNER, "store", identity, token)["deleted"]


@pytest.mark.parametrize("mutation", ["quantity", "archive", "event", "store_name", "household_name", "membership", "notes", "schema"])
def test_preview_is_invalidated_by_changed_snapshot(inventory, mutation):
    storage = inventory["storage"]
    home, store, active = inventory["home"], inventory["stores"][0], inventory["active"]
    preview = storage.preview_deletion(OWNER, "store", store.id)
    if mutation == "quantity":
        storage.change_item_quantity(store.id, OWNER, active.id, 1)
    elif mutation == "archive":
        storage.archive_item(store.id, OWNER, active.id)
    elif mutation == "store_name":
        storage.rename_store(OWNER, inventory["stores"][1].id, "Synthetic renamed store")
    elif mutation == "household_name":
        storage.rename_household(OWNER, home.id, "Synthetic renamed home")
    else:
        with closing(storage._connect()) as connection, connection:
            if mutation == "event":
                connection.execute("INSERT INTO item_events (item_id, chat_id, action, note) VALUES (?, ?, 'synthetic', 'Changed event')", (active.id, OWNER))
            elif mutation == "membership":
                connection.execute("INSERT INTO household_members (household_id, chat_id) VALUES (?, ?)", (home.id, OUTSIDER))
            elif mutation == "notes":
                connection.execute("UPDATE inventory_items SET notes = 'Synthetic changed note' WHERE id = ?", (active.id,))
            else:
                connection.execute("ALTER TABLE inventory_items ADD COLUMN synthetic_review_field TEXT")
    before = product_state(storage)
    with pytest.raises(ValueError, match="Данные изменились"):
        storage.delete_reviewed(OWNER, "store", store.id, preview["preview_token"])
    assert product_state(storage) == before and token_exists(storage, preview["preview_token"])


def test_expiry_and_preview_gc_do_not_delete_inventory(inventory, monkeypatch):
    storage = inventory["storage"]
    identity = inventory["active"].id
    preview = storage.preview_deletion(OWNER, "item", identity)
    monkeypatch.setattr(inventory_deletion.time, "time", lambda: preview["expires_at"])
    before = product_state(storage)
    with pytest.raises(ValueError, match="истёк"):
        storage.delete_reviewed(OWNER, "item", identity, preview["preview_token"])
    new_preview = storage.preview_deletion(OWNER, "item", identity)
    assert not token_exists(storage, preview["preview_token"])
    assert token_exists(storage, new_preview["preview_token"])
    assert product_state(storage) == before


def test_last_store_is_rejected_in_new_path_without_changing_legacy_behavior(inventory):
    storage, stores = inventory["storage"], inventory["stores"]
    preview = storage.preview_deletion(OWNER, "store", stores[0].id)
    assert storage.remove_store(OWNER, stores[1].id)
    assert storage.remove_store(OWNER, stores[2].id)
    before = product_state(storage)
    with pytest.raises(ValueError, match="Последний склад"):
        storage.preview_deletion(OWNER, "store", stores[0].id)
    with pytest.raises(ValueError, match="Последний склад"):
        storage.delete_reviewed(OWNER, "store", stores[0].id, preview["preview_token"])
    assert product_state(storage) == before and token_exists(storage, preview["preview_token"])
    assert storage.remove_store(OWNER, stores[0].id)


@pytest.mark.parametrize("target_type", ["item", "store", "household"])
def test_views_and_current_selections_are_repaired_in_same_transaction(inventory, target_type):
    storage, home, stores = inventory["storage"], inventory["home"], inventory["stores"]
    storage.set_current_store(OWNER, stores[0].id)
    storage.set_current_store(MEMBER, stores[0].id)
    identity = target_id(inventory, target_type)
    preview = storage.preview_deletion(OWNER, target_type, identity)
    # UI refreshes during review are not inventory changes and must be safe.
    storage.set_active_view(OWNER, 1, "synthetic-item", item_id=inventory["archived"].id)
    storage.set_active_view(MEMBER, 2, "synthetic-store", household_id=home.id, store_id=stores[0].id)
    storage.set_active_view(OUTSIDER, 3, "synthetic-foreign", household_id=inventory["foreign"].id, item_id=inventory["foreign_item"].id)
    before_users = {actor: storage.get_user(actor) for actor in (OWNER, MEMBER)}
    storage.delete_reviewed(OWNER, target_type, identity, preview["preview_token"])
    assert storage.get_active_view(OWNER) is None
    assert storage.get_active_view(OUTSIDER) is not None
    if target_type == "item":
        assert storage.get_active_view(MEMBER) is not None
        assert {actor: storage.get_user(actor) for actor in before_users} == before_users
    elif target_type == "store":
        assert storage.get_active_view(MEMBER) is None
        for actor in (OWNER, MEMBER):
            assert storage.get_user(actor).current_household_id == home.id
            assert storage.get_user(actor).current_store_id == stores[1].id
    else:
        assert storage.get_active_view(MEMBER) is None
        for actor, fallback in ((OWNER, inventory["owner_fallback"]), (MEMBER, inventory["member_fallback"])):
            user = storage.get_user(actor)
            assert user.current_household_id == fallback.id
            assert user.current_store_id in {store.id for store in storage.list_stores(fallback.id)}
    assert_foreign_keys(storage)


def test_deletion_failure_rolls_back_views_data_and_token(inventory):
    storage, store = inventory["storage"], inventory["stores"][0]
    storage.set_active_view(OWNER, 1, "synthetic-store", store_id=store.id)
    preview = storage.preview_deletion(OWNER, "store", store.id)
    member_preview = storage.preview_deletion(MEMBER, "item", inventory["active"].id)
    home_preview = storage.preview_deletion(OWNER, "household", inventory["home"].id)
    with closing(storage._connect()) as connection, connection:
        connection.execute(f"CREATE TRIGGER synthetic_abort BEFORE DELETE ON stores WHEN old.id = {store.id} BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    before = product_state(storage)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        storage.delete_reviewed(OWNER, "store", store.id, preview["preview_token"])
    assert product_state(storage) == before and token_exists(storage, preview["preview_token"])
    assert token_exists(storage, member_preview["preview_token"])
    assert token_exists(storage, home_preview["preview_token"])
    with closing(storage._connect()) as connection, connection:
        connection.execute("DROP TRIGGER synthetic_abort")
    assert storage.delete_reviewed(OWNER, "store", store.id, preview["preview_token"])["deleted"]


def test_success_invalidates_all_related_reviews_before_exact_identity_recreation(inventory):
    storage, item = inventory["storage"], inventory["active"]
    used = storage.preview_deletion(OWNER, "item", item.id)
    unused_owner = storage.preview_deletion(OWNER, "item", item.id)
    unused_member = storage.preview_deletion(MEMBER, "item", item.id)
    ancestor_store = storage.preview_deletion(OWNER, "store", item.store_id)
    ancestor_home = storage.preview_deletion(OWNER, "household", item.household_id)
    foreign = storage.preview_deletion(OUTSIDER, "item", inventory["foreign_item"].id)
    with closing(storage._connect()) as connection:
        saved_item = dict(connection.execute("SELECT * FROM inventory_items WHERE id = ?", (item.id,)).fetchone())
        saved_events = [dict(row) for row in connection.execute("SELECT * FROM item_events WHERE item_id = ? ORDER BY id", (item.id,))]
    before = product_state(storage)
    assert storage.delete_reviewed(OWNER, "item", item.id, used["preview_token"])["deleted"]
    for preview in (used, unused_owner, unused_member, ancestor_store, ancestor_home):
        assert not token_exists(storage, preview["preview_token"])
    assert token_exists(storage, foreign["preview_token"])
    # Recreate the complete prior state, including event IDs and timestamps.
    # A digest-only check would now accept these unused old tokens.
    with closing(storage._connect()) as connection, connection:
        for table, rows in (("inventory_items", [saved_item]), ("item_events", saved_events)):
            for row in rows:
                columns = ", ".join('"' + column + '"' for column in row)
                placeholders = ", ".join("?" for _ in row)
                connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row.values()))
    assert product_state(storage) == before
    for actor, preview in ((OWNER, unused_owner), (MEMBER, unused_member)):
        with pytest.raises(ValueError, match="Предпросмотр недействителен"):
            storage.delete_reviewed(actor, "item", item.id, preview["preview_token"])
    assert product_state(storage) == before
    assert storage.delete_reviewed(OUTSIDER, "item", inventory["foreign_item"].id, foreign["preview_token"])["deleted"]
    assert_foreign_keys(storage)


@pytest.mark.parametrize("deleted_type", ["store", "household"])
def test_parent_deletion_invalidates_descendant_reviews_for_all_actors(inventory, deleted_type):
    storage = inventory["storage"]
    item_preview = storage.preview_deletion(MEMBER, "item", inventory["active"].id)
    store_preview = storage.preview_deletion(MEMBER, "store", inventory["stores"][0].id)
    home_preview = storage.preview_deletion(OWNER, "household", inventory["home"].id)
    foreign = storage.preview_deletion(OUTSIDER, "item", inventory["foreign_item"].id)
    identity = target_id(inventory, deleted_type)
    used = storage.preview_deletion(OWNER, deleted_type, identity)
    assert storage.delete_reviewed(OWNER, deleted_type, identity, used["preview_token"])["deleted"]
    for preview in (item_preview, store_preview, home_preview, used):
        assert not token_exists(storage, preview["preview_token"])
    assert token_exists(storage, foreign["preview_token"])
    assert_foreign_keys(storage)


def test_concurrent_use_of_same_preview_can_delete_only_once(inventory):
    storage, item = inventory["storage"], inventory["active"]
    preview = storage.preview_deletion(OWNER, "item", item.id)
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait(timeout=5)
        try:
            return storage.delete_reviewed(OWNER, "item", item.id, preview["preview_token"])["deleted"]
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: attempt(), range(2))) == [False, True]
    assert storage.get_item(item.id) is None
    assert_foreign_keys(storage)


def test_preview_table_is_an_additive_migration_without_product_changes(inventory):
    storage = inventory["storage"]
    with closing(storage._connect()) as connection, connection:
        connection.execute("DROP TABLE deletion_previews")
    before = product_state(storage)
    reopened = Storage(storage.path)
    assert product_state(reopened) == before
    assert reopened.preview_deletion(OWNER, "item", inventory["active"].id)["preview_token"]

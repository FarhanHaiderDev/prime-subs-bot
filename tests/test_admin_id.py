"""#26: ADMIN_CHAT_ID is validated at startup and must be a positive numeric user id."""

import pytest

import bot


@pytest.mark.parametrize("value", ["1", "111", "7852284044", " 42 ", "9" * 16])
def test_valid_user_ids_are_accepted_and_normalised(value):
    assert bot.validate_admin_chat_id(value) == value.strip()


@pytest.mark.parametrize(
    "value,hint",
    [
        (None, "not set"),
        ("", "not set"),
        ("   ", "not set"),
        ("0", "without a sign or leading zeros"),
        ("007", "without a sign or leading zeros"),
        ("+5", "without a sign or leading zeros"),
        ("-100123456789", "group or channel"),
        ("-5", "group or channel"),
        ("@myadmin", "usernames are not accepted"),
        ("admin", "usernames are not accepted"),
        ("12.5", "digits only"),
        ("12abc", "digits only"),
        ("1 2", "digits only"),
        ("9" * 17, "digits only"),
    ],
)
def test_invalid_values_are_rejected_with_a_clear_message(value, hint):
    with pytest.raises(ValueError) as error:
        bot.validate_admin_chat_id(value)

    assert "ADMIN_CHAT_ID" in str(error.value) and hint in str(error.value)


def test_the_message_never_echoes_the_value():
    with pytest.raises(ValueError) as error:
        bot.validate_admin_chat_id("-1009876543210")
    assert "9876543210" not in str(error.value)


@pytest.mark.parametrize("value", ["-100123456789", "@admin", "0", ""])
async def test_startup_refuses_an_invalid_admin_id_before_touching_anything(monkeypatch, value):
    monkeypatch.setattr(bot, "ADMIN_CHAT_ID", value)
    monkeypatch.setattr(bot, "storage", None)

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("startup went past the ADMIN_CHAT_ID check")

    monkeypatch.setattr(bot.Storage, "from_env", must_not_be_called)
    monkeypatch.setattr(bot.CredentialBox, "from_env", must_not_be_called)

    with pytest.raises(SystemExit) as exit_info:
        await bot.main()

    assert "ADMIN_CHAT_ID" in str(exit_info.value)


def test_a_valid_id_is_stored_normalised_for_the_admin_check(monkeypatch):
    monkeypatch.setattr(bot, "ADMIN_CHAT_ID", " 555 ")
    bot.apply_admin_chat_id()

    assert bot.ADMIN_CHAT_ID == "555"
    assert bot.is_admin(555) and not bot.is_admin(556)

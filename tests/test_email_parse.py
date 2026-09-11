import base64

from app.email_parse import ParsedEmail, parse_message


def _gmail_payload(message_id: str = "msgid123", thread_id: str = "thread456") -> dict:
    body = base64.urlsafe_b64encode(b"hello jobs").decode()
    return {
        "id": message_id,
        "threadId": thread_id,
        "snippet": "hello jobs",
        "internalDate": "1756962000000",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"},
                {"name": "Subject", "value": "8 new jobs match your preferences"},
                {"name": "Message-ID", "value": "<abc123@mail.gmail.com>"},
            ],
            "body": {"data": body},
        },
    }


def test_gmail_link_uses_thread_id_not_message_id():
    email = parse_message(_gmail_payload())
    assert email.id == "msgid123"
    assert email.thread_id == "thread456"
    assert email.gmail_link == "https://mail.google.com/mail/u/0/#all/thread456"
    assert "#inbox/msgid123" not in email.gmail_link


def test_gmail_search_link_uses_rfc822_message_id():
    email = parse_message(_gmail_payload())
    assert email.rfc822_message_id == "<abc123@mail.gmail.com>"
    assert email.gmail_search_link.startswith("https://mail.google.com/mail/u/0/#search/rfc822msgid:")
    assert "abc123" in email.gmail_search_link
    assert "%40" in email.gmail_search_link or "@" in email.gmail_search_link


def test_gmail_link_falls_back_to_message_id():
    email = ParsedEmail(id="only-msg")
    assert email.gmail_link == "https://mail.google.com/mail/u/0/#all/only-msg"
    assert email.gmail_search_link == ""


def test_body_ignores_a_stub_plain_text_preheader():
    email = ParsedEmail(
        id="blast",
        text="\u200c ",
        html="""
        <html><body>
          <p>Naveen, Deloitte is interested in you</p>
          <p>Machine Learning Engineer &mdash; Seattle, WA</p>
          <p>Data Scientist &mdash; Austin, TX</p>
        </body></html>
        """,
    )
    body = email.body()
    assert "Machine Learning Engineer" in body
    assert "Data Scientist" in body


def test_body_keeps_a_real_plain_text_part():
    plain = "Hi Naveen,\n\n" + ("We reviewed your application in detail. " * 20)
    email = ParsedEmail(id="note", text=plain, html="<html><body>ignored</body></html>")
    assert email.body().startswith("Hi Naveen,")
    assert "ignored" not in email.body()


def test_body_survives_a_short_email_with_no_html():
    email = ParsedEmail(id="short", text="Are you open to a contract role?")
    assert email.body() == "Are you open to a contract role?"

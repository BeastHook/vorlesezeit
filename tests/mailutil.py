from email.message import EmailMessage


def plain_text(message: EmailMessage) -> str:
    return message.get_body(preferencelist=("plain",)).get_content()


def html_text(message: EmailMessage) -> str:
    return message.get_body(preferencelist=("html",)).get_content()

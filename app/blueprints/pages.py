"""Top-level page route."""

from flask import Blueprint, render_template

import pipeline as pl

bp = Blueprint("pages", __name__)


@bp.route("/")
def index():
    return render_template("index.html", gmail_sender_address=pl.GMAIL_SENDER_ADDRESS)

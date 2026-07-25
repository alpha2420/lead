"""ICP generation, buyer-ICP generation, and chat refinement routes."""

import logging

from flask import Blueprint, jsonify, request

import pipeline as pl

bp = Blueprint("icp", __name__, url_prefix="/api")
logger = logging.getLogger(__name__)


@bp.route("/generate-icp", methods=["POST"])
def generate_icp_only():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    inquiry = (data.get("inquiry") or "").strip()
    if not inquiry:
        return jsonify({"error": "inquiry is required"}), 400

    try:
        icp = pl.parse_inquiry(inquiry)
        return jsonify({"status": "success", "icp": icp})
    except Exception as e:
        logger.error("Failed to generate ICP: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@bp.route("/generate-icp-from-website", methods=["POST"])
def generate_icp_from_website():
    """Scrapes a company's own website and infers the ICP of the customers
    that company should be targeting. See pl.parse_website_to_icp()."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    website = (data.get("website") or "").strip()
    if not website:
        return jsonify({"error": "website is required"}), 400

    try:
        result = pl.parse_website_to_icp(website)
        return jsonify({
            "status": "success",
            "icp": result.get("icp"),
            "source_url": result.get("source_url"),
            "site_title": result.get("site_title"),
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        logger.error("Failed to generate ICP from website: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@bp.route("/generate-buyer-icp", methods=["POST"])
def generate_buyer_icp():
    """
    Inverted ICP mode: given a description of a dataset/audience, returns
    who would BUY that dataset (not the audience itself). "icp" is directly
    usable by /api/run-custom to run a real pipeline search; "buyer_report"
    is the fuller 12-section brief for display/export only.
    """
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    description = (data.get("description") or "").strip()
    if not description:
        return jsonify({"error": "description is required"}), 400

    try:
        result = pl.parse_buyer_inquiry(description)
        return jsonify({
            "status": "success",
            "icp": result.get("icp"),
            "buyer_report": result.get("buyer_report"),
        })
    except Exception as e:
        logger.error("Failed to generate buyer ICP: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@bp.route("/generate-buyer-icp-from-website", methods=["POST"])
def generate_buyer_icp_from_website():
    """Scrapes a website (e.g. a data vendor's product page) and infers WHO
    WOULD BUY the dataset/audience it describes. See pl.parse_buyer_inquiry_from_website()."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    website = (data.get("website") or "").strip()
    if not website:
        return jsonify({"error": "website is required"}), 400

    try:
        result = pl.parse_buyer_inquiry_from_website(website)
        return jsonify({
            "status": "success",
            "icp": result.get("icp"),
            "buyer_report": result.get("buyer_report"),
            "source_url": result.get("source_url"),
            "site_title": result.get("site_title"),
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        logger.error("Failed to generate buyer ICP from website: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@bp.route("/chat-icp", methods=["POST"])
def chat_icp_route():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    message = (data.get("message") or "").strip()
    history = data.get("history") or []
    current_icp = data.get("icp")

    if not message:
        return jsonify({"error": "message is required"}), 400

    try:
        result = pl.chat_icp(message, history, current_icp)
        return jsonify({
            "status": "success",
            "chat_response": result.get("chat_response"),
            "suggested_replies": result.get("suggested_replies") or ["+ Founder", "+ SaaS", "+ United States"],
            "icp": result.get("icp")
        })
    except Exception as e:
        logger.error("Failed in chat_icp_route: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500

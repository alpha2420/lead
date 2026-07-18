"""ICP generation, buyer-ICP generation, coverage estimation, and chat
refinement routes."""

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


@bp.route("/estimate-coverage", methods=["POST"])
def estimate_coverage_route():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    icp = data.get("icp")
    if not icp:
        return jsonify({"error": "icp object is required"}), 400
    try:
        target = int(data.get("target", 25))
    except (TypeError, ValueError):
        return jsonify({"error": "target must be a number"}), 400

    try:
        estimate = pl.estimate_coverage(icp, target)
        return jsonify({"status": "success", "estimate": estimate})
    except Exception as e:
        logger.error("Failed to estimate coverage: %s", e)
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

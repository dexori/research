"""Authenticated preview and image endpoints, mounted by the application factory.

Authentication/CSRF stay in the app guard. These routes only read validated
assets; they never accept arbitrary filesystem paths or external image URLs.
"""
import io
from pathlib import Path

from flask import abort, jsonify, request, send_file

from draft_import import list_drafts
from git_sync import UUID
from markdown_rendering import render_md
from research_images import MAX_IMAGE_BYTES, asset_path, inspect_image


def register_media_routes(app, data_dir, get_db):
    directory = Path(data_dir)
    db = get_db

    @app.post('/markdown/preview')
    def markdown_preview():
        content = request.form.get('content','')
        if len(content) > 200000:
            return jsonify(error='预览正文超过 20 万字，请拆分记录。'), 400
        return jsonify(html=str(render_md(content)))

    def send_image(raw):
        try:
            info = inspect_image(raw)
        except ValueError:
            abort(415, '附件不是有效的静态 PNG、JPEG 或 WebP 图片。')
        response = send_file(io.BytesIO(raw), mimetype=info['mimetype'], max_age=0)
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    @app.get('/media/<identifier>')
    def image_media(identifier):
        if not UUID.fullmatch(identifier):
            abort(404)
        row = db().execute('SELECT storage_name FROM attachments WHERE storage_name=?',(identifier,)).fetchone()
        if not row:
            abort(404)
        path = directory/'uploads'/row['storage_name']
        if path.is_symlink() or not path.is_file():
            abort(404)
        with path.open('rb') as stream:
            return send_image(stream.read(MAX_IMAGE_BYTES+1))

    @app.get('/draft-media/<identifier>')
    def draft_media(identifier):
        if not UUID.fullmatch(identifier):
            abort(404)
        # A real, validated draft must own this asset; knowing a filename is insufficient.
        for draft in list_drafts(directory, limit=500):
            for image in draft.get('images',[]):
                if image['id']==identifier:
                    try:
                        with asset_path(directory, identifier, image['extension']).open('rb') as stream:
                            return send_image(stream.read(MAX_IMAGE_BYTES+1))
                    except OSError:
                        abort(404)
        abort(404)

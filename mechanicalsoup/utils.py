class LinkNotFoundError(Exception):
    """Exception raised when mechanicalsoup fails to find something.

    This happens in situations like (non-exhaustive list):

    * :func:`~mechanicalsoup.StatefulBrowser.find_link` is called, but
      no link is found.

    * The browser was configured with raise_on_404=True and a 404
      error is triggered while browsing.

    * The user tried to fill-in a field which doesn't exist in a form
      (e.g. browser["name"] = "val" with browser being a
      StatefulBrowser).
    """
    pass


def is_file_input(tag):
    """Return whether ``tag`` is an ``<input type="file">`` control."""
    return tag.name == "input" and str(tag.get("type", "")).lower() == "file"


def is_submit_control(tag):
    """Return whether ``tag`` is a button/input that can submit a form.

    Both ``<input type="submit">`` and ``<button>`` (other than explicit
    ``type="button"`` or ``type="reset"`` buttons) are submit controls.
    """
    if tag.name == "input":
        return str(tag.get("type", "")).lower() == "submit"
    if tag.name == "button":
        button_type = tag.get("type")
        return (not isinstance(button_type, str) or
                button_type.lower() not in ("button", "reset"))
    return False


def is_multipart_file_upload(form, tag):
    return (
        form.get("enctype", "") == "multipart/form-data" and
        is_file_input(tag)
    )


def is_disabled(tag):
    """Return whether a form control is disabled per the HTML specification.

    A control is disabled if it carries a ``disabled`` attribute, or if it is
    a descendant of a ``<fieldset>`` element whose ``disabled`` attribute is
    set -- except when it is inside that fieldset's first ``<legend>`` child,
    which stays enabled. Disabled controls are barred from submission, so
    browsers do not include their name/value pairs in the form data.

    https://html.spec.whatwg.org/multipage/form-elements.html#concept-fieldset-disabled
    """
    if tag.has_attr("disabled"):
        return True
    for fieldset in tag.find_parents("fieldset"):
        if not fieldset.has_attr("disabled"):
            continue
        # Controls inside the disabled fieldset's first <legend> child are
        # not disabled by that fieldset.
        legend = fieldset.find("legend", recursive=False)
        if legend is not None and any(p is legend for p in tag.parents):
            continue
        return True
    return False

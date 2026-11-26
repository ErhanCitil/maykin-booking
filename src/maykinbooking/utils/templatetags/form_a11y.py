from django import template

register = template.Library()


@register.simple_tag
def accessible_widget(field, has_errors=None, errors_id=None):
    """
    The widget of a bound form field. When the field is invalid, it gets
    ``aria-invalid="true"`` and ``aria-describedby`` pointing at the element with
    the error message, so screen readers announce it with the field.

    By default the field is invalid when it has errors, and the error message
    has the id ``<field id>_errors``. Pass ``has_errors`` and ``errors_id`` when
    the errors of several fields are shown together.
    """
    if has_errors is None:
        has_errors = bool(field.errors)
    if not has_errors:
        return field.as_widget()
    return field.as_widget(
        attrs={
            "aria-invalid": "true",
            "aria-describedby": errors_id or f"{field.auto_id}_errors",
        }
    )

"""Read-only local launch assessment. No secret reads, DB setup, server or external requests."""
from .configuration import CHECKS, PilotError, load_config
from .package import dependencies


def local_ocr_ready():
    try:
        from pathlib import Path
        from travelcards.ocr import russian_tesseract_available, recognize_card_photo
        if not russian_tesseract_available():return False
        fixture=Path(__file__).resolve().parents[1]/'docs/fixtures/photo-ocr/printed-exif-1.jpg'
        fields,metadata=recognize_card_photo(fixture.read_bytes())
        return (metadata.get('status')=='recognized' and fields['budget']['value']=='180000'
                and fields['budget']['state']=='ocr_candidate' and fields['children_count']['state']=='unknown')
    except Exception:
        return False


def assess(root, config_path):
    checks=dependencies(root)
    ocr=local_ocr_ready()
    blockers=[]
    if not checks['python_3_12']:blockers.append('python_3_12_required')
    if not checks['virtual_environment']:blockers.append('virtual_environment_required')
    if checks['missing_or_different_packages']:blockers.append('package_dependencies_missing')
    if not ocr:blockers.append('local_russian_ocr_unavailable')
    config=None
    try:config=load_config(config_path)
    except (PilotError,OSError):blockers.append('configuration_missing_or_invalid')
    if config:
        blockers.extend('not_confirmed_'+key for key in CHECKS if not config.confirmations[key])
        if not config.approval_reference:blockers.append('approval_reference_missing')
        if not config.telegram_enabled:blockers.append('telegram_disabled')
    # Local booleans are declarations, not a network or account verification.
    return {**checks,'ocr_rus_available':ocr,'ocr_printed_fixture_passed':ocr,'configuration_valid':config is not None,
        'local_checks_passed':not blockers,'blockers':blockers,
        'external_checks_required':['approved_isolated_host_and_https','private_ui_and_health_proxy',
            'verified_bot_and_private_user_chat','webhook_registration_and_delivery_test'],
        'database_checked':False,'secrets_read':False,'network_used':False,'server_started':False,
        'live_telegram_verified':False,'ocr_quality_verified':False}

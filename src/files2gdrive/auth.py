"""Desktop Google OAuth with tokens stored only in Linux Secret Service.

No credential or authorization URL is logged. Optional factory/backend arguments
are dependency-injection hooks for tests; production uses Google SDK and the
explicit SecretService backend, never keyring's automatic backend selection.
"""
import json
from pathlib import Path

DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.file'
SCOPES = (DRIVE_SCOPE,)
SERVICE_NAME = 'Files2GDrive.GoogleOAuth'


class AuthenticationError(RuntimeError):
    """Safe-to-display authentication failure, without token/server details."""


class TokenStoreError(AuthenticationError):
    """Secret Service is unavailable or could not persist credentials."""


class KeyringTokenStore:
    def __init__(self, computer_id, *, backend=None):
        if not isinstance(computer_id, str) or not computer_id.strip():
            raise ValueError('A persistent computer ID is required')
        self.account = computer_id
        if backend is None:
            try:
                from keyring.backends.SecretService import Keyring
                backend = Keyring()
                # Accessing priority verifies Secret Service availability.
                if backend.priority <= 0:
                    raise RuntimeError('Secret Service unavailable')
            except Exception:
                raise TokenStoreError('Unlock or enable the Ubuntu credential store before authenticating') from None
        self.backend = backend

    def get(self):
        try:
            return self.backend.get_password(SERVICE_NAME, self.account)
        except Exception:
            raise TokenStoreError('Cannot read the Ubuntu credential store') from None

    def set(self, value):
        if not isinstance(value, str):
            raise TypeError('Credential data must be a string')
        try:
            self.backend.set_password(SERVICE_NAME, self.account, value)
            if self.backend.get_password(SERVICE_NAME, self.account) != value:
                raise RuntimeError('Credential storage verification failed')
        except Exception:
            raise TokenStoreError('Cannot save credentials in the Ubuntu credential store') from None

    def delete(self):
        try:
            if self.backend.get_password(SERVICE_NAME, self.account) is not None:
                self.backend.delete_password(SERVICE_NAME, self.account)
        except Exception:
            raise TokenStoreError('Cannot remove credentials from the Ubuntu credential store') from None


def _client_config(path):
    try:
        configuration = json.loads(Path(path).read_text(encoding='utf-8'))
        installed = configuration['installed']
        if not isinstance(installed, dict) or 'web' in configuration:
            raise ValueError('Not a Desktop app configuration')
        if not isinstance(installed.get('client_id'), str) or not installed['client_id'].endswith('.apps.googleusercontent.com'):
            raise ValueError('Invalid Google OAuth client')
        if not isinstance(installed.get('client_secret'), str) or not installed['client_secret']:
            raise ValueError('Missing client secret')
        if installed.get('auth_uri') not in (
            'https://accounts.google.com/o/oauth2/auth',
            'https://accounts.google.com/o/oauth2/v2/auth',
        ) or installed.get('token_uri') != 'https://oauth2.googleapis.com/token':
            raise ValueError('Unsupported OAuth endpoints')
        return configuration
    except (OSError, ValueError, TypeError, KeyError):
        raise AuthenticationError('Provide a valid Google Desktop app OAuth client JSON') from None


def _flow_factory(configuration, scopes):
    from google_auth_oauthlib.flow import InstalledAppFlow
    return InstalledAppFlow.from_client_config(configuration, scopes=list(scopes),
                                               autogenerate_code_verifier=True)


def _credentials_factory(info, scopes):
    from google.oauth2.credentials import Credentials
    return Credentials.from_authorized_user_info(info, scopes=list(scopes))


def _request_factory():
    from google.auth.transport.requests import Request
    return Request()


class AuthManager:
    def __init__(self, client_file, token_store, *, flow_factory=None,
                 credentials_factory=None, request_factory=None):
        self.configuration = _client_config(client_file)
        self.token_store = token_store
        self.flow_factory = flow_factory or _flow_factory
        self.credentials_factory = credentials_factory or _credentials_factory
        self.request_factory = request_factory or _request_factory

    def _save(self, credentials):
        if not credentials.refresh_token:
            raise AuthenticationError('Google did not grant offline access; authenticate again')
        self.token_store.set(credentials.to_json())

    def login(self):
        # Probe the store before launching a browser, failing closed if locked.
        self.token_store.get()
        try:
            flow = self.flow_factory(self.configuration, SCOPES)
            credentials = flow.run_local_server(
                host='127.0.0.1', port=0, open_browser=True,
                authorization_prompt_message='',
                success_message='Files2GDrive conectado. Voce pode fechar esta janela.',
                timeout_seconds=180, access_type='offline', prompt='consent',
            )
            if not credentials or not credentials.valid:
                raise AuthenticationError('Google authentication was not completed')
            granted = getattr(credentials, 'granted_scopes', None)
            if granted is not None and DRIVE_SCOPE not in granted:
                raise AuthenticationError('Google Drive permission was not granted')
            self._save(credentials)
            return credentials
        except AuthenticationError:
            raise
        except Exception:
            raise AuthenticationError('Google authentication failed or timed out; try again') from None

    def credentials(self):
        raw = self.token_store.get()
        if raw is None:
            raise AuthenticationError('Authenticate your Google account first')
        try:
            info = json.loads(raw)
            client = self.configuration['installed']
            if (info.get('client_id') != client['client_id']
                    or info.get('token_uri') != client['token_uri']):
                raise AuthenticationError('Stored credentials belong to another OAuth client; authenticate again')
            stored_scopes = info.get('scopes')
            if stored_scopes is not None and (not isinstance(stored_scopes, list) or DRIVE_SCOPE not in stored_scopes):
                raise AuthenticationError('Stored credentials lack Google Drive permission; authenticate again')
            credentials = self.credentials_factory(info, SCOPES)
            if not credentials.valid:
                if not credentials.refresh_token:
                    raise AuthenticationError('Authenticate your Google account again')
                credentials.refresh(self.request_factory())
                if not credentials.valid:
                    raise AuthenticationError('Authenticate your Google account again')
                self._save(credentials)
            return credentials
        except AuthenticationError:
            raise
        except Exception:
            raise AuthenticationError('Cannot renew Google access; reconnect or authenticate again') from None

    def logout(self):
        """Remove local credentials only; does not revoke access or delete backups."""
        self.token_store.delete()

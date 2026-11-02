# Bandeja, notificações e sessão Ubuntu

## Uso

Abrir **Files2GDrive** pelo menu de aplicativos ou executar:

```bash
.venv/bin/python -m files2gdrive --state-dir /caminho/estado desktop
```

O lançador reutiliza o motor existente ou inicia um novo, abre a configuração e mantém um único helper da bandeja. Não cadastra pastas automaticamente. No início da sessão, `desktop --background` faz o mesmo sem abrir o navegador.

Menu da bandeja:

- **Configuração:** gera um novo link local autenticado e abre o navegador padrão.
- **Pausar backup:** preserva fila/prazos e a pausa persiste entre sessões.
- **Retomar backup:** retoma o trabalho permitido; não ignora uma conferência de reconexão pendente.
- **Sair:** solicita encerramento do motor e fecha o ícone após confirmação. Não há supervisor que reinicie imediatamente o motor após essa ação. Reabrir o aplicativo ou entrar na próxima sessão inicia novamente.

Fechar a página do navegador não encerra o motor. O encerramento aguarda a operação em andamento conforme as limitações de cancelamento já documentadas.

Também disponíveis:

```bash
.venv/bin/python -m files2gdrive --state-dir /caminho/estado open
.venv/bin/python -m files2gdrive --state-dir /caminho/estado quit
```

A porta pode ser atribuída automaticamente e mudar ao reiniciar. Preferir abrir pelo ícone/atalho em vez de guardar um endereço com porta fixa.

## Inicialização e atalho

```bash
.venv/bin/python -m files2gdrive --state-dir /caminho/estado install-session
.venv/bin/python -m files2gdrive --state-dir /caminho/estado remove-session
```

A instalação por usuário cria:

- `~/.local/share/applications/files2gdrive.desktop` — menu de aplicativos.
- `~/.config/autostart/files2gdrive.desktop` — início da sessão, sem abrir navegador.

Diretórios XDG personalizados são respeitados. Os comandos não sobrescrevem entradas desconhecidas, de outro estado ou links simbólicos. Remover a integração não exclui dados locais, tokens ou backups. O comando não encerra um processo que já esteja ativo.

Na execução a partir do código-fonte, os atalhos pessoais apontam para o Python do ambiente virtual e para o diretório de estado escolhido. Mover esse ambiente exige reinstalar os atalhos. O pacote `.deb` usa caminhos de instalação permanentes.

## Dependências nativas

Ubuntu 24.04/GNOME:

- `python3-gi`, `gir1.2-gtk-3.0`, `gir1.2-ayatanaappindicator3-0.1`.
- Suporte AppIndicator do painel; no GNOME, `gnome-shell-extension-appindicator`.
- `libnotify-bin`, com `notify-send`, para notificações.

Para desenvolvimento, o ambiente virtual precisa ter acesso a PyGObject instalado pelo Ubuntu, por exemplo com `--system-site-packages`. O pacote declara as dependências nativas; a extensão AppIndicator também precisa estar carregada na sessão gráfica.

Se GTK/bandeja estiver indisponível, o motor permanece independente. O helper tenta manter notificações, registra o problema e a interface continua acessível pelo lançador. Recuperação automática de todos os tipos de queda do motor/helper ainda precisa de refinamento antes da versão final.

## Notificações

Avisos acionáveis para autenticação necessária, espaço no Drive, acesso recusado e raiz local inacessível. Não mostram conteúdo de arquivos ou caminhos privados. Cada condição é avisada uma vez enquanto persistir, inclusive entre reinícios; quando resolvida e depois reaparecer, pode gerar novo aviso. Falha na entrega aplica espera antes de tentar de novo.

Falta de espaço é identificada pelo código `storageQuotaExceeded`; limites temporários de requisições não são apresentados como disco cheio.

## Segurança e isolamento

A bandeja não abre o SQLite e não recebe tokens Google. Ela usa um socket Unix privado no estado, com permissões 0600, diretório 0700 e verificação do UID do cliente/servidor. Somente comandos status, pause, resume, open e quit são aceitos, com mensagens limitadas. Abertura do navegador só aceita a URL loopback produzida pelo próprio motor.

Um lock impede duplicação do helper; outro protege o motor. Um processo antigo usando o estado sem o canal novo não é encerrado automaticamente pelo lançador: é necessário fechá-lo antes de iniciar a versão nova.

## Validação e limitações

Há testes automatizados para o canal de controle, notificações e integração de sessão. A disponibilidade da bandeja depende do ambiente gráfico e do suporte a AppIndicator. Após instalar dependências ou extensões em uma sessão já aberta, pode ser necessário reiniciar o aplicativo ou a sessão.

Encerramento e pausa são atendidos entre grupos de envio. Transferências já em andamento podem precisar concluir antes que o comando seja aplicado.

Referências: [PyGObject](https://pygobject.gnome.org/), [Ayatana AppIndicator](https://github.com/AyatanaIndicators/libayatana-appindicator), [Desktop Entry Exec](https://specifications.freedesktop.org/desktop-entry-spec/latest/exec-variables.html), [XDG Autostart](https://specifications.freedesktop.org/autostart-spec/latest/), [notificações desktop](https://specifications.freedesktop.org/notification/latest/protocol.html).

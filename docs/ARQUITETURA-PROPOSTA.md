# Arquitetura — base aprovada

Pesquisa em 2026-11-01. Stack e organização remota aprovadas na rodada 8. Detalhes de implementação e validações pendentes abaixo continuam sujeitos a definição. O nome deste arquivo foi mantido para preservar os links existentes.

## Stack recomendada

- Python 3.12 como base para o Ubuntu 24.04, com dependências isoladas e versões fixadas no pacote.
- watchdog para eventos locais via inotify, complementado por varredura inicial, na retomada e periódica para reconciliação.
- SQLite para cadastro de pastas, associação de arquivos a IDs remotos, fila persistente, retenção e estado de transferências.
- FastAPI/Uvicorn para API e página de configuração apenas em loopback; HTML, CSS e JavaScript simples para a interface inicial.
- Cliente oficial Google API para Python, OAuth desktop e uploads retomáveis.
- Helper de bandeja separado, em Python/PyGObject com Ayatana AppIndicator, sujeito a validação no ambiente gráfico alvo.
- Serviço do usuário para o motor, sem executar o backup como root; integração de sessão gráfica para a bandeja.
- Pacote Debian `.deb`, incluindo inicializadores, ícone e dependências necessárias.

Python é recomendado pela integração com o desktop Linux, bibliotecas Google e facilidade de manutenção. Go também é viável e facilita distribuir o motor compilado, mas a bandeja continua exigindo integração nativa. Não há benchmark que justifique escolher uma linguagem por desempenho neste projeto; a transferência e o armazenamento remoto são partes centrais da carga.

## Separação de responsabilidades

1. Motor: monitora, reconcilia, agenda, transfere e aplica retenção, independentemente do navegador e da bandeja.
2. Interface local: configura pastas, filtros e conta; apresenta fila, progresso, logs e restauração.
3. Bandeja: mostra estado e oferece menu por clique direito com Configuração, Pausar backup, Retomar backup e Sair. Sair encerra motor e bandeja preservando fila e prazos; pausa explícita persiste entre reinícios até retomada. Clique duplo não é requisito.
4. Adaptador Drive: autenticação, pastas remotas, upload, movimentação, metadados e lixeira.

Fila e prazos devem sobreviver a reinícios. Repetições de operações precisam evitar duplicações. Uma varredura incompleta ou uma raiz inacessível não autoriza exclusões em massa. O motor deve verificar mudanças durante uploads, agrupar eventos e reagendar conteúdo instável.

Proteger a interface local contra requisições de outros sites, com autenticação de sessão, validação de origem/host e proteção contra CSRF. Guardar tokens no cofre da sessão quando disponível; não colocá-los no frontend, nos logs ou no banco sem proteção. Detalhes do fallback serão definidos antes da implementação.

## Organização remota sugerida

`<Pasta escolhida>/<Computador>/<Pasta monitorada>/...`

Usar identificadores persistentes por computador e pasta, com nomes exibidos configuráveis, para evitar colisões. Layout exato de `Excluídos` e dos metadados auxiliares ainda precisa de especificação. Metadados devem existir remotamente para permitir restauração após perda do banco local, sem expor credenciais. Na primeira versão, preservar permissões comuns e data de modificação, sem proprietário/grupo, ACLs, atributos estendidos ou bits especiais. Reconexão após reinstalação exige escolha explícita do backup, mapeamento e conferência das pastas antes de inferir exclusões.

## OAuth e distribuição

- Cliente OAuth do tipo Desktop app, navegador padrão, PKCE e callback loopback.
- Projeto Google Cloud mantido pelo distribuidor; destinatários apenas autenticam suas próprias contas.
- Escopo inicial proposto: `drive.file`, para arquivos criados ou explicitamente autorizados ao app. Não permite acesso geral a arquivos arbitrários do Drive.
- Criar a pasta de backup pelo aplicativo; seleção de pasta preexistente exigiria um fluxo de autorização apropriado.
- A distribuição oficial usa projeto OAuth em produção, conforme confirmação do mantenedor em 2 de novembro de 2026. Projetos próprios de desenvolvimento em Testing continuam sujeitos às regras desse modo.
- Identidade pública, suporte, homepage e políticas estão publicados em `https://files2gdrive.erservices.top/`, com HTTPS obrigatório e propriedade do domínio verificada no Google Search Console. Publicação do OAuth não equivale automaticamente à aprovação de toda verificação de marca aplicável.
- Validar restauração e redescoberta dos backups em uma instalação nova usando o mesmo cliente de produção.

## Limitações e validações pendentes

- Ambiente gráfico do usuário ainda desconhecido; validar disponibilidade da bandeja e abertura do menu por clique direito.
- AppIndicator é orientado a menu: oferecer `Configuração` e um atalho no menu de aplicativos. Clique duplo dispensado pelo usuário. Ubuntu Noble disponibiliza bindings Ayatana e extensão AppIndicator; Go systray também depende de GTK/Ayatana no Linux.
- Não houve implementação, benchmark ou teste do pacote nesta pesquisa.
- Fixar versões exatas das bibliotecas ao iniciar a implementação.
- Definir formato/versionamento de metadados, atributos Linux suportados, recuperação após reinstalação e restauração segura de caminhos e permissões.
- Definir comportamento ao mudar filtros, mover itens entre raízes e editar nomes de destinos.

## Fontes oficiais consultadas

- [Google Drive — Python quickstart](https://developers.google.com/workspace/drive/api/quickstart/python)
- [Google Drive — uploads retomáveis](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
- [Google — OAuth para apps instalados](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Google Drive — escopos](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Google — preparação para produção](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview)
- [Google — expiração de tokens](https://developers.google.com/identity/protocols/oauth2#expiration)
- [Google — verificação de marca](https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification)
- [watchdog](https://python-watchdog.readthedocs.io/en/latest/watchmedo_cli.html)
- [FastAPI — servidor](https://fastapi.tiangolo.com/deployment/manually/)
- [Python — rotação de logs](https://docs.python.org/3/library/logging.handlers.html)
- [fsnotify — alternativa Go e limitações de monitoramento](https://pkg.go.dev/github.com/fsnotify/fsnotify)
- [PyGObject — instalação](https://pygobject.gnome.org/getting_started.html)
- [Go systray — dependências Linux](https://github.com/getlantern/systray#linux)
- [Ubuntu Noble — extensão AppIndicator](https://packages.ubuntu.com/noble/gnome-shell-extension-appindicator)
- [Ubuntu Noble — bindings Ayatana](https://launchpad.net/ubuntu/noble/+package/gir1.2-ayatanaappindicator3-0.1)

# Files2GDrive

[Site do aplicativo](https://files2gdrive.erservices.top/) · [Privacidade](https://files2gdrive.erservices.top/privacidade/) · [Termos de uso](https://files2gdrive.erservices.top/termos/)

Backup contínuo de arquivos locais do Ubuntu para o Google Drive, com configuração pelo navegador e controles na bandeja do sistema.

![Visão geral do Files2GDrive, com Google Drive autenticado e nenhuma operação pendente](docs/images/visao-geral.png)

**Release pública v0.1.1 para Ubuntu 24.04 LTS, amd64 e Python 3.12.** Esta é uma versão de correção da série inicial `0.x`, que ainda representa a evolução do projeto; consulte as limitações e mantenha outras cópias de arquivos importantes.

## Funcionalidades

- Monitora alterações locais e mantém os arquivos correspondentes no Drive atualizados.
- Seleciona pastas por checkboxes, com navegação pela pasta pessoal, diretórios ocultos e exceções por subpasta. As alterações são revisadas antes de aplicar.
- Exclui arquivos do envio por padrões globais ou por pasta, incluindo extensões e coringas `*` e `?`.
- Mantém arquivos removidos localmente em **Excluídos por 30 dias**; depois, envia-os à lixeira do Drive. Não faz exclusão definitiva.
- Usa fila persistente, novas tentativas com espera progressiva, até quatro uploads simultâneos e preparação de diretórios em lotes.
- Exibe autenticação e progresso, permite pausar e retomar e oferece logs locais de operações.
- Abre a configuração pelo menu da bandeja, com opções para pausar, retomar e sair.
- Inclui consulta e restauração de backups e reconexão com conferência dos dados existentes.

A sincronização é **Ubuntu → Google Drive**. Alterações no Drive não são copiadas de volta ao computador. Edições locais substituem o conteúdo da cópia corrente; o aplicativo não mantém histórico próprio de versões.

## Instalar

Baixe o pacote `.deb` na [release v0.1.1](https://github.com/eduardoellery/Files2GDrive/releases/tag/v0.1.1) e execute no diretório do arquivo:

```bash
sudo apt install ./files2gdrive_0.1.1_amd64.deb
```

O APT instala as dependências nativas; as dependências Python já estão incluídas no pacote. Não é necessário instalar `pip` ou `watchdog` separadamente.

Abra **Files2GDrive** no menu de aplicativos. Na configuração inicial:

1. Clique em **Conectar ao Google** e autorize sua própria conta.
2. Escolha os nomes da pasta principal e do computador no Drive.
3. Marque as pastas locais, revise os efeitos e aplique a seleção.

O destino segue a organização:

```text
Pasta principal / Nome do computador / Pastas selecionadas
```

A pasta principal padrão é `Files2GDrive`. O nome inicial do computador vem do hostname e pode ser escolhido pelo usuário antes do primeiro backup. Alterar esses nomes depois de iniciar os envios ainda não está disponível na interface.

Para atualizar o pacote, saia pelo menu da bandeja antes de instalar a nova versão. Veja [instalação, atualização e remoção](docs/INSTALACAO-DEB.md) e [integração com a sessão Ubuntu](docs/INTEGRACAO-DESKTOP.md).

### Conta Google e OAuth

Cada instalação usa uma conta, autenticada pelo próprio usuário no navegador. O pacote do distribuidor pode incluir a identificação OAuth Desktop do aplicativo; ele não inclui autorização, senha ou tokens de usuários. Os tokens são guardados no cofre Secret Service da sessão Ubuntu.

O projeto OAuth da distribuição oficial está **em produção**, conforme atualização do mantenedor em 2 de novembro de 2026. A autenticação não depende mais de adicionar cada conta à lista de testadores. O domínio `erservices.top` teve a propriedade verificada no Google Search Console, e o site público usa HTTPS obrigatório com certificado válido.

Contas Google Workspace continuam sujeitas às políticas e à aprovação do administrador da organização. Quando a autorização precisar ser renovada, a fila permanece salva até a reconexão. Projetos OAuth próprios ainda em **Testing** seguem as restrições de testadores desse modo. O status OAuth em produção é independente da versão do aplicativo e não equivale, por si só, à conclusão de toda verificação de marca do Google.

Quem desenvolver ou redistribuir o aplicativo precisa configurar seu cliente OAuth Desktop e habilitar a Drive API. Consulte o [guia de integração com o Google Drive](docs/GUIA-GOOGLE-DRIVE.md). Não publique tokens, perfis de execução ou arquivos de credenciais pessoais.

## Executar pelo código-fonte

Use Python 3.12 no Ubuntu 24.04. Para a bandeja e o cofre da sessão:

```bash
sudo apt install python3-venv python3-gi gir1.2-gtk-3.0 \
  gir1.2-ayatanaappindicator3-0.1 libnotify-bin xdg-utils gnome-keyring
git clone https://github.com/eduardoellery/Files2GDrive.git
cd Files2GDrive
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install -e .
python -m files2gdrive --help
```

O acesso aos pacotes do sistema no ambiente virtual permite usar os bindings GTK instalados pelo APT. No GNOME, a bandeja também depende da extensão AppIndicator habilitada na sessão; veja o guia de integração Ubuntu.

### Teste local sem Google

Um perfil novo iniciado pelo código-fonte usa armazenamento simulado por padrão. Escolha uma pasta pequena de teste, fora do diretório de estado:

```bash
python -m files2gdrive --state-dir /tmp/files2gdrive-demo add /caminho/pasta-de-teste
python -m files2gdrive --state-dir /tmp/files2gdrive-demo serve
```

O navegador abre a interface local. `Ctrl+C` encerra a execução e preserva o estado. O simulador não foi feito para grandes volumes: ele representa o conteúdo dos arquivos em JSON/base64.

Para usar a bandeja em vez de iniciar somente a interface web:

```bash
python -m files2gdrive --state-dir /caminho/estado desktop
```

### Perfil de desenvolvimento com Google Drive

Com um cliente OAuth Desktop preparado conforme o guia, configure um perfil separado da simulação:

```bash
python -m files2gdrive --state-dir /caminho/estado-drive configure-drive \
  --client-file /caminho/cliente-oauth-desktop.json \
  --folder-name 'Meu Backup' --computer-name 'Meu computador'
python -m files2gdrive --state-dir /caminho/estado-drive login
python -m files2gdrive --state-dir /caminho/estado-drive desktop
```

`configure-drive --help` e os demais subcomandos mostram as opções disponíveis. Os comandos diretos que acessam o banco exigem o motor parado; com o aplicativo em execução, use a interface web ou os controles da bandeja. Não execute dois motores sobre o mesmo perfil.

### Organização do ambiente de desenvolvimento

Use um único checkout Git para editar, testar e preparar publicações. Mantenha
os dados privados fora do repositório; por exemplo:

```text
~/projetos/Files2GDrive/                # checkout Git: código, testes e docs
    .venv/                            # ambiente local, ignorado pelo Git
    dist/                             # artefatos gerados, ignorados pelo Git
~/.local/share/files2gdrive-dev/        # diretório privado, permissão 0700
    profile/                          # banco, fila, logs e estado de execução
    oauth/                            # cliente OAuth local
    notes/                            # decisões e resumo privado de retomada
    test-profiles/                    # perfis de testes manuais
    backups/                          # cópias privadas de segurança
```

Selecione o perfil explicitamente com `--state-dir`. O aplicativo instalado pelo
`.deb` usa seu próprio perfil; não compartilhe o mesmo perfil ou identidade de
backup entre motores ativos. Ao mover um perfil, encerre o motor antes, preserve
sua identidade e o cofre de credenciais e atualize o caminho do cliente OAuth e
os atalhos pessoais. Consulte a [integração com Ubuntu](docs/INTEGRACAO-DESKTOP.md).

Faça alterações em branches e publique por pull requests. Distribua apenas os
arquivos selecionados pelo exportador e pelo empacotador, após revisão; o
`.gitignore` é uma proteção adicional, não substitui essa conferência. Notas e
backups privados não fazem parte do código-fonte nem dos anexos das releases.

## Limitações atuais

- O pacote é destinado ao **Ubuntu 24.04 amd64**. Ubuntu 26.04 e outras arquiteturas ainda não têm suporte validado.
- Pastas vazias, links simbólicos e arquivos especiais não são enviados.
- O envio usa snapshots privados temporários em disco; podem existir até quatro ao mesmo tempo. Arquivos grandes exigem espaço temporário disponível.
- Arquivos acima de 5 MiB usam envio em blocos, mas a sessão de upload não é persistida entre reinícios: uma nova tentativa pode reenviar o arquivo inteiro.
- Pausa e saída são atendidas entre grupos de envio; operações em andamento podem precisar concluir primeiro.
- Não há snapshot conjunto para bancos de dados em uso ou vários arquivos que precisem permanecer consistentes entre si.
- A velocidade varia com tamanho, número de pastas, latência, conexão e limites do Drive. Não há garantia de tempo por arquivo.

## Versionamento

Usamos [versionamento semântico](https://semver.org/lang/pt-BR/) no formato `MAJOR.MINOR.PATCH`, com tags Git `vMAJOR.MINOR.PATCH`. A release pública inicial foi `v0.1.0`; a correção atual está em `v0.1.1`. Isso não declara a API da série `0.x` estável. Novas funcionalidades usam versões como `0.2.0`. Mudanças são registradas no [histórico de versões](CHANGELOG.md); as instruções de atualização também aparecem nas notas de cada release. Artefatos já publicados não são substituídos por conteúdos diferentes sob a mesma versão.

## Testes

No ambiente virtual:

```bash
python -m unittest discover -s tests -v
```

A suíte usa diretórios temporários e dublês da API, sem precisar autenticar uma conta Google. Abrange fila, retenção, filtros, reconexão, interface local, IDs após falhas e processamento concorrente. O mantenedor também confirmou o fluxo manual de desinstalação, remoção do estado de teste, nova autorização Google e reinstalação com backup em outro Ubuntu.

## Documentação

- [Estatísticas e consentimento do site público](docs/ANALYTICS.md)

- [Instalador Debian](docs/INSTALACAO-DEB.md)
- [Integração com Ubuntu](docs/INTEGRACAO-DESKTOP.md)
- [Interface web local](docs/INTERFACE-WEB.md)
- [Integração Google Drive](docs/GUIA-GOOGLE-DRIVE.md)
- [Restauração e reconexão](docs/RESTAURACAO-E-RECONEXAO.md)
- [Arquitetura](docs/ARQUITETURA-PROPOSTA.md)
- [Requisitos](docs/REQUISITOS.md)

## Licença

Distribuído sob a [licença MIT](LICENSE). Você pode usar, modificar e redistribuir o código, inclusive comercialmente, desde que preserve o aviso de copyright e o texto da licença nas cópias ou partes substanciais do software. Dependências de terceiros mantêm suas próprias licenças.

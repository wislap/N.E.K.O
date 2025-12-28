use std::cmp::Ordering;
use std::fs;
use std::io;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc::Receiver;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use arboard::Clipboard;
use crossterm::event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use crossterm::execute;
use crossterm::terminal::{Clear, ClearType};
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen};
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Gauge, List, ListItem, Paragraph, Wrap};
use ratatui::Terminal;
use ratatui::{backend::CrosstermBackend, Frame};

use crate::core;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Screen {
    Home,
    Exec,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Tab {
    Select,
    Mode,
    Path,
    Run,
    Output,
}

impl Tab {
    fn title(self) -> &'static str {
        match self {
            Tab::Select => "Select / 选择",
            Tab::Mode => "Mode / 模式",
            Tab::Path => "Path / 路径",
            Tab::Run => "Run / 执行",
            Tab::Output => "Output / 输出",
        }
    }
}

fn load_pack_list(app: &mut App) -> Result<()> {
    let repo_root = if let Some(r) = &app.args.root {
        r.clone()
    } else {
        core::find_repo_root(std::env::current_dir().context("failed to get cwd")?)?
    };
    let plugins_dir = repo_root.join("plugin").join("plugins");
    let ids = core::list_packable_plugin_ids(&plugins_dir)?;
    app.pack_items = ids;
    app.pack_selected = vec![true; app.pack_items.len()];
    app.pack_cursor = 0;
    Ok(())
}

fn init_path_root(app: &mut App) -> Result<()> {
    if !matches!(app.cmd, CmdKind::Pack | CmdKind::Unpack) {
        return Ok(());
    }
    let base = if let Some(root) = &app.args.root {
        root.clone()
    } else {
        std::env::current_dir().context("failed to get cwd for path picker")?
    };
    app.path_current_dir = base;
    refresh_path_entries(app)
}

fn sync_path_to_args(app: &mut App) -> Result<()> {
    if !matches!(app.cmd, CmdKind::Pack | CmdKind::Unpack) {
        return Ok(());
    }

    match app.cmd {
        CmdKind::Pack => {
            if let Some(dest) = &app.args.dest {
                if dest.is_dir() {
                    app.path_current_dir = dest.clone();
                    refresh_path_entries(app)?;
                    app.path_cursor = 0;
                }
            }
        }
        CmdKind::Unpack => {
            if let Some(zip_path) = app.args.zip_path.clone() {
                if let Some(parent) = zip_path.parent() {
                    app.path_current_dir = parent.to_path_buf();
                    refresh_path_entries(app)?;

                    if let Some(file_name) = zip_path.file_name().and_then(|n| n.to_str()) {
                        if let Some(idx) = app
                            .path_entries
                            .iter()
                            .position(|e| !e.is_dir && e.is_zip && e.name == file_name)
                        {
                            app.path_cursor = idx;
                        } else {
                            app.path_cursor = 0;
                        }
                    }
                }
            }
        }
        _ => {}
    }

    Ok(())
}

fn refresh_path_entries(app: &mut App) -> Result<()> {
    app.path_entries.clear();
    app.path_cursor = 0;

    if !matches!(app.cmd, CmdKind::Pack | CmdKind::Unpack) {
        return Ok(());
    }

    let dir = &app.path_current_dir;
    if !dir.is_dir() {
        return Ok(());
    }

    let mut entries: Vec<PathEntry> = Vec::new();

    // Parent directory entry
    if let Some(parent) = dir.parent() {
        if parent != dir { // avoid infinite at fs root
            entries.push(PathEntry {
                name: "..".to_string(),
                is_dir: true,
                is_zip: false,
                is_parent: true,
            });
        }
    }

    let mut children: Vec<_> = fs::read_dir(dir)
        .with_context(|| format!("failed to read dir {}", dir.display()))?
        .collect::<Result<Vec<_>, _>>()?;

    children.sort_by(|a, b| a.file_name().cmp(&b.file_name()));

    for e in children {
        let path = e.path();
        let name = e.file_name().to_string_lossy().to_string();
        let md = e.metadata().with_context(|| format!("failed to stat {}", path.display()))?;
        if md.is_dir() {
            entries.push(PathEntry {
                name,
                is_dir: true,
                is_zip: false,
                is_parent: false,
            });
        } else if md.is_file() {
            if matches!(app.cmd, CmdKind::Unpack) {
                let is_zip = name.to_lowercase().ends_with(".zip");
                if is_zip {
                    entries.push(PathEntry {
                        name,
                        is_dir: false,
                        is_zip: true,
                        is_parent: false,
                    });
                }
            }
        }
    }

    // Directories first, then files (zip)
    entries.sort_by(|a, b| match (a.is_dir, b.is_dir) {
        (true, false) => Ordering::Less,
        (false, true) => Ordering::Greater,
        _ => a.name.cmp(&b.name),
    });

    app.path_entries = entries;
    Ok(())
}

fn toggle_pack_cursor(app: &mut App) {
    if app.pack_items.is_empty() {
        return;
    }
    if app.pack_cursor >= app.pack_selected.len() {
        return;
    }
    app.pack_selected[app.pack_cursor] = !app.pack_selected[app.pack_cursor];
}

fn selected_pack_ids(app: &App) -> Vec<String> {
    if app.pack_items.is_empty() {
        return Vec::new();
    }
    let mut out = Vec::new();
    for (i, id) in app.pack_items.iter().enumerate() {
        if *app.pack_selected.get(i).unwrap_or(&false) {
            out.push(id.clone());
        }
    }
    // None selected means pack all -> return empty so CLI uses default.
    if out.is_empty() {
        Vec::new()
    } else {
        out
    }
}

fn draw_pack_select(f: &mut Frame<'_>, app: &App, area: Rect, highlight: bool) {
    let mut items: Vec<ListItem> = Vec::new();

    let total = app.pack_items.len();
    if total == 0 {
        let p = Paragraph::new("(no packable plugins)")
            .block(Block::default().borders(Borders::ALL).title("Pack Select / 打包选择"));
        f.render_widget(p, area);
        return;
    }

    // Approximate visible rows inside the bordered list
    let capacity = area
        .height
        .saturating_sub(2) // top/bottom borders
        .max(1) as usize;
    let cursor = app.pack_cursor.min(total.saturating_sub(1));
    let start = if total <= capacity {
        0
    } else if cursor < capacity {
        0
    } else if cursor >= total - capacity {
        total - capacity
    } else {
        cursor + 1 - capacity
    };

    for (i, id) in app
        .pack_items
        .iter()
        .enumerate()
        .skip(start)
        .take(capacity)
    {
        let checked = *app.pack_selected.get(i).unwrap_or(&false);
        let mark = if checked { "[x]" } else { "[ ]" };
        let line = format!("{} {}", mark, id);
        let style = if i == app.pack_cursor {
            Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)
        } else {
            Style::default()
        };
        items.push(ListItem::new(Line::from(Span::styled(line, style))));
    }

    let title = "Pack Select / 打包选择  (↑↓ move, Space toggle, a all, x none)";
    let border_style = if highlight {
        Style::default().fg(Color::Green)
    } else {
        Style::default()
    };
    let list = List::new(items)
        .block(Block::default().borders(Borders::ALL).border_style(border_style).title(title));
    f.render_widget(list, area);
}

fn draw_path_panel(f: &mut Frame<'_>, app: &App, area: Rect, highlight: bool) {
    let mut items: Vec<ListItem> = Vec::new();
    let cwd = app.path_current_dir.display().to_string();
    items.push(ListItem::new(Line::from(Span::styled(
        cwd,
        Style::default().fg(Color::Cyan),
    ))));
    items.push(ListItem::new(Line::from("")));

    let total = app.path_entries.len();
    if total == 0 {
        items.push(ListItem::new(Line::from("(empty directory)")));
    } else {
        // Inner list height is area.height - 2 (borders). Reserve 2 lines (cwd + blank),
        // use remaining rows for entries.
        let capacity = area
            .height
            .saturating_sub(4) // 2 borders + 2 header lines
            .max(1) as usize;
        let cursor = app.path_cursor.min(total.saturating_sub(1));
        let start = if total <= capacity {
            0
        } else if cursor < capacity {
            0
        } else if cursor >= total - capacity {
            total - capacity
        } else {
            cursor + 1 - capacity
        };

        // For Unpack, remember which zip file is currently selected (if any)
        let selected_zip_name: Option<String> = if matches!(app.cmd, CmdKind::Unpack) {
            app.args
                .zip_path
                .as_ref()
                .and_then(|p| p.file_name())
                .map(|s| s.to_string_lossy().to_string())
        } else {
            None
        };

        for (i, ent) in app
            .path_entries
            .iter()
            .enumerate()
            .skip(start)
            .take(capacity)
        {
            let is_selected_zip = matches!(app.cmd, CmdKind::Unpack)
                && ent.is_zip
                && selected_zip_name
                    .as_ref()
                    .map(|n| n == &ent.name)
                    .unwrap_or(false);

            let prefix = if ent.is_parent {
                "[..]"
            } else if ent.is_dir {
                "[DIR]"
            } else if ent.is_zip && is_selected_zip {
                "[ZIP*]"
            } else if ent.is_zip {
                "[ZIP]"
            } else {
                "[   ]"
            };
            let mut style = Style::default();
            if app.focus && i == app.path_cursor {
                style = style.fg(Color::Yellow).add_modifier(Modifier::BOLD);
            }
            let text = format!("{} {}", prefix, ent.name);
            items.push(ListItem::new(Line::from(Span::styled(text, style))));
        }
    }

    let title = match app.cmd {
        CmdKind::Pack => "Path / 路径 (Pack 输出目录: ↑↓ move, Space 进入/选择)",
        CmdKind::Unpack => "Path / 路径 (Unpack 输入 .zip: ↑↓ move, Space 进入/选择)",
        _ => "Path / 路径",
    };
    let border_style = if highlight {
        Style::default().fg(Color::Green)
    } else {
        Style::default()
    };
    let list = List::new(items)
        .block(Block::default().borders(Borders::ALL).border_style(border_style).title(title));
    f.render_widget(list, area);
}

fn available_tabs(app: &App) -> Vec<Tab> {
    match app.cmd {
        CmdKind::Info => vec![Tab::Run, Tab::Output],
        CmdKind::Pack => vec![Tab::Select, Tab::Mode, Tab::Path, Tab::Run, Tab::Output],
        CmdKind::Unpack => vec![Tab::Mode, Tab::Path, Tab::Run, Tab::Output],
        CmdKind::Check => vec![Tab::Mode, Tab::Run, Tab::Output],
    }
}

fn draw_exec(f: &mut Frame<'_>, app: &App, area: Rect) {
    let tabs = available_tabs(app);
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(22), Constraint::Min(0)])
        .split(area);

    // Left button bar
    let left = cols[0];
    let right = cols[1];
    let items = tabs
        .iter()
        .enumerate()
        .map(|(i, t)| {
            let active = i == app.tab_active;
            let selected = i == app.tab_selected;
            let mut style = Style::default();
            if active {
                style = style.fg(Color::Cyan).add_modifier(Modifier::BOLD);
            }
            if selected {
                style = style.bg(Color::DarkGray);
            }
            ListItem::new(Line::from(Span::styled(t.title(), style)))
        })
        .collect::<Vec<_>>();
    let left_border_style = if !app.focus {
        Style::default().fg(Color::Green)
    } else {
        Style::default()
    };
    let list = List::new(items).block(
        Block::default()
            .borders(Borders::ALL)
            .border_style(left_border_style)
            .title(format!("{}", app.cmd.title())),
    );
    f.render_widget(list, left);

    // Right detail pane
    let active_tab = tabs.get(app.tab_active).copied().unwrap_or(Tab::Run);
    let right_highlight = app.focus;
    match active_tab {
        Tab::Select => {
            if matches!(app.cmd, CmdKind::Pack) {
                draw_pack_select(f, app, right, right_highlight);
            } else {
                let p = Paragraph::new("No selection for this command")
                    .block(
                        Block::default()
                            .borders(Borders::ALL)
                            .border_style(if right_highlight {
                                Style::default().fg(Color::Green)
                            } else {
                                Style::default()
                            }),
                    );
                f.render_widget(p, right);
            }
        }
        Tab::Mode => {
            draw_mode_panel(f, app, right, right_highlight);
        }
        Tab::Path => {
            draw_path_panel(f, app, right, right_highlight);
        }
        Tab::Run => {
            draw_run(f, app, right, right_highlight);
        }
        Tab::Output => {
            draw_output_panel(f, app, right, right_highlight);
        }
    }
}

fn draw_mode_panel(f: &mut Frame<'_>, app: &App, area: Rect, highlight: bool) {
    let items = mode_items(app)
        .into_iter()
        .enumerate()
        .map(|(i, (label, value))| {
            let mark = if value { "[x]" } else { "[ ]" };
            let text = format!("{} {}", mark, label);
            let mut style = Style::default();
            if app.focus && i == app.mode_cursor {
                style = style.fg(Color::Yellow).add_modifier(Modifier::BOLD);
            }
            ListItem::new(Line::from(Span::styled(text, style)))
        })
        .collect::<Vec<_>>();

    let title = if app.focus { "Mode / 模式 (focused: ↑↓ Space, ← exit)" } else { "Mode / 模式 (Enter/→ to focus)" };
    let border_style = if highlight {
        Style::default().fg(Color::Green)
    } else {
        Style::default()
    };
    let list = List::new(items)
        .block(Block::default().borders(Borders::ALL).border_style(border_style).title(title));
    f.render_widget(list, area);
}

fn mode_items(app: &App) -> Vec<(String, bool)> {
    match app.cmd {
        CmdKind::Pack => vec![("no_md5".to_string(), app.args.no_md5)],
        CmdKind::Unpack => vec![("force".to_string(), app.args.force)],
        CmdKind::Check => vec![
            ("python".to_string(), app.args.python),
            ("python_strict".to_string(), app.args.python_strict),
        ],
        CmdKind::Info => Vec::new(),
    }
}

fn mode_items_len(app: &App) -> usize {
    mode_items(app).len()
}

fn toggle_mode_at_cursor(app: &mut App) {
    match app.cmd {
        CmdKind::Pack => {
            if app.mode_cursor == 0 {
                app.args.no_md5 = !app.args.no_md5;
            }
        }
        CmdKind::Unpack => {
            if app.mode_cursor == 0 {
                app.args.force = !app.args.force;
            }
        }
        CmdKind::Check => {
            if app.mode_cursor == 0 {
                app.args.python = !app.args.python;
            } else if app.mode_cursor == 1 {
                app.args.python_strict = !app.args.python_strict;
            }
        }
        CmdKind::Info => {}
    }
}

fn draw_output_panel(f: &mut Frame<'_>, app: &App, area: Rect, highlight: bool) {
    let border_style = if highlight {
        Style::default().fg(Color::Green)
    } else {
        Style::default()
    };
    let out = Paragraph::new(app.output.clone())
        .block(Block::default().borders(Borders::ALL).border_style(border_style).title("Output / 输出"))
        .wrap(Wrap { trim: false });
    f.render_widget(out, area);
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CmdKind {
    Info,
    Pack,
    Unpack,
    Check,
}

impl CmdKind {
    fn title(self) -> &'static str {
        match self {
            CmdKind::Info => "info",
            CmdKind::Pack => "pack",
            CmdKind::Unpack => "unpack",
            CmdKind::Check => "check",
        }
    }
}

#[derive(Debug, Default, Clone)]
struct CmdArgs {
    root: Option<PathBuf>,
    plugin_id: Option<String>,
    zip_path: Option<PathBuf>,
    dest: Option<PathBuf>,
    force: bool,
    python: bool,
    python_strict: bool,
    no_md5: bool,
}

struct App {
    screen: Screen,
    selected: usize,
    tab_selected: usize,
    tab_active: usize,
    focus: bool,
    mode_cursor: usize,
    last_home_click: Option<(Instant, usize)>,
    last_quit_key: Option<(Instant, char)>,
    cmd: CmdKind,
    args: CmdArgs,
    running: bool,
    started_at: Option<Instant>,
    spinner_i: usize,
    output: String,
    last_status: Option<i32>,
    task_rx: Option<Receiver<anyhow::Result<std::process::Output>>>,

    pack_items: Vec<String>,
    pack_selected: Vec<bool>,
    pack_cursor: usize,

    path_entries: Vec<PathEntry>,
    path_cursor: usize,
    path_current_dir: PathBuf,

    clipboard: Option<Clipboard>,

    show_help: bool,
}

#[derive(Debug, Clone)]
struct PathEntry {
    name: String,
    is_dir: bool,
    is_zip: bool,
    is_parent: bool,
}

pub fn run(repo_root: Option<PathBuf>) -> Result<()> {
    enable_raw_mode().context("enable raw mode")?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture, Clear(ClearType::All)).ok();
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend).context("create terminal")?;

    let mut app = App {
        screen: Screen::Home,
        selected: 0,
        tab_selected: 0,
        tab_active: 0,
        focus: false,
        mode_cursor: 0,
        last_home_click: None,
        last_quit_key: None,
        cmd: CmdKind::Info,
        args: CmdArgs {
            root: repo_root.clone(),
            ..CmdArgs::default()
        },
        running: false,
        started_at: None,
        spinner_i: 0,
        output: String::new(),
        last_status: None,
        task_rx: None,

        pack_items: Vec::new(),
        pack_selected: Vec::new(),
        pack_cursor: 0,

        path_entries: Vec::new(),
        path_cursor: 0,
        path_current_dir: repo_root
            .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))),

        clipboard: Clipboard::new().ok(),

        show_help: false,
    };

    let tick_rate = Duration::from_millis(100);

    loop {
        terminal.draw(|f| draw(f, &app))?;

        if event::poll(tick_rate).unwrap_or(false) {
            match event::read()? {
                Event::Key(k) if k.kind == KeyEventKind::Press => {
                    if handle_key(&mut app, k)? {
                        break;
                    }
                }
                Event::Mouse(m) => {
                    if let Ok(size) = terminal.size() {
                        let root = Rect::new(0, 0, size.width, size.height);
                        let chunks = Layout::default()
                            .direction(Direction::Vertical)
                            .constraints([
                                Constraint::Length(3),
                                Constraint::Min(0),
                                Constraint::Length(3),
                            ])
                            .split(root);
                        let body = chunks[1];
                        handle_mouse(&mut app, m, body);
                    }
                }
                _ => {}
            }
        } else {
            // tick
            if app.running {
                app.spinner_i = app.spinner_i.wrapping_add(1);
            }
        }

        // poll background task
        if app.running {
            if let Some(rx) = &app.task_rx {
                match rx.try_recv() {
                    Ok(res) => {
                        app.running = false;
                        app.task_rx = None;
                        match res {
                            Ok(out) => {
                                let mut s = String::new();
                                s.push_str(&String::from_utf8_lossy(&out.stdout));
                                if !out.stderr.is_empty() {
                                    if !s.ends_with('\n') {
                                        s.push('\n');
                                    }
                                    s.push_str(&String::from_utf8_lossy(&out.stderr));
                                }
                                app.output = s;
                                app.last_status = out.status.code();
                            }
                            Err(e) => {
                                app.output = format!("failed to run command: {e}");
                                app.last_status = Some(1);
                            }
                        }
                    }
                    Err(std::sync::mpsc::TryRecvError::Empty) => {}
                    Err(_) => {
                        app.running = false;
                        app.task_rx = None;
                    }
                }
            }
        }
    }

    disable_raw_mode().ok();
    let mut stdout = io::stdout();
    execute!(stdout, LeaveAlternateScreen, DisableMouseCapture).ok();
    Ok(())
}

fn handle_key(app: &mut App, key: KeyEvent) -> Result<bool> {
    // Ctrl-based global shortcuts
    if key.modifiers.contains(KeyModifiers::CONTROL) {
        match key.code {
            // Copy Output via Ctrl-Y or Ctrl-Insert
            KeyCode::Char('y') | KeyCode::Char('Y') | KeyCode::Insert => {
                if matches!(app.screen, Screen::Exec) {
                    let tabs = available_tabs(app);
                    let active_tab = tabs.get(app.tab_active).copied().unwrap_or(Tab::Run);
                    if matches!(active_tab, Tab::Output) {
                        copy_output_to_clipboard(app);
                    }
                }
                // do not treat as quit
                return Ok(false);
            }
            // Double Ctrl-C / Ctrl-Q to exit
            KeyCode::Char('c') | KeyCode::Char('C') => {
                let now = Instant::now();
                if let Some((last_t, last_ch)) = app.last_quit_key {
                    if last_ch == 'c' && now.duration_since(last_t) <= Duration::from_secs(2) {
                        return Ok(true);
                    }
                }
                app.last_quit_key = Some((now, 'c'));
                return Ok(false);
            }
            KeyCode::Char('q') | KeyCode::Char('Q') => {
                let now = Instant::now();
                if let Some((last_t, last_ch)) = app.last_quit_key {
                    if last_ch == 'q' && now.duration_since(last_t) <= Duration::from_secs(2) {
                        return Ok(true);
                    }
                }
                app.last_quit_key = Some((now, 'q'));
                return Ok(false);
            }
            _ => {}
        }
    }
    // any non-quit, non-ctrl key clears pending quit
    app.last_quit_key = None;

    let code = key.code;

    // Plain 'q' toggles help overlay (not quit). When help is open, only 'q' or Esc closes it.
    if app.show_help {
        match code {
            KeyCode::Char('q') | KeyCode::Char('Q') | KeyCode::Esc => {
                app.show_help = false;
            }
            _ => {}
        }
        return Ok(false);
    }

    if matches!(code, KeyCode::Char('q') | KeyCode::Char('Q')) {
        app.show_help = true;
        return Ok(false);
    }

    // Esc: back from Exec to Home (no quit)
    if matches!(code, KeyCode::Esc) {
        if matches!(app.screen, Screen::Exec) {
            app.screen = Screen::Home;
        }
        return Ok(false);
    }

    match app.screen {
        Screen::Home => match code {
            KeyCode::Up => app.selected = app.selected.saturating_sub(1),
            KeyCode::Down => app.selected = (app.selected + 1).min(3),
            KeyCode::Enter => {
                app.cmd = match app.selected {
                    0 => CmdKind::Info,
                    1 => CmdKind::Pack,
                    2 => CmdKind::Unpack,
                    _ => CmdKind::Check,
                };
                app.screen = Screen::Exec;
                app.tab_selected = 0;
                app.tab_active = 0;
                app.focus = false;
                app.mode_cursor = 0;
                app.output.clear();
                app.last_status = None;
                if matches!(app.cmd, CmdKind::Pack) {
                    load_pack_list(app)?;
                }
                if matches!(app.cmd, CmdKind::Pack | CmdKind::Unpack) {
                    // Initialize browser base dir, then sync to any existing dest/zip selection
                    init_path_root(app)?;
                    sync_path_to_args(app)?;
                }
            }
            _ => {}
        },

        Screen::Exec => {
            let tabs = available_tabs(app);
            if tabs.is_empty() {
                return Ok(false);
            }
            let active_tab = tabs.get(app.tab_active).copied().unwrap_or(Tab::Run);
            let focusable = matches!(active_tab, Tab::Select | Tab::Mode | Tab::Path);

            match code {
                // Enter/Right enters focus for focusable tabs. Left/Enter exits focus.
                KeyCode::Enter | KeyCode::Right => {
                    if focusable {
                        app.focus = true;
                        match active_tab {
                            Tab::Mode => {
                                app.mode_cursor = 0;
                            }
                            Tab::Path => {
                                // When focusing Path, make sure view matches any existing dest/zip
                                let _ = sync_path_to_args(app);
                            }
                            _ => {}
                        }
                    }
                }
                KeyCode::Left => {
                    app.focus = false;
                }

                // Not focused: Up/Down switches active tab.
                KeyCode::Up if !app.focus => {
                    app.tab_selected = app.tab_selected.saturating_sub(1);
                    app.tab_active = app.tab_selected;
                }
                KeyCode::Down if !app.focus => {
                    app.tab_selected = (app.tab_selected + 1).min(tabs.len() - 1);
                    app.tab_active = app.tab_selected;
                }

                // Focused Select: Up/Down move, Space toggles.
                KeyCode::Up if app.focus && matches!(active_tab, Tab::Select) && matches!(app.cmd, CmdKind::Pack) => {
                    app.pack_cursor = app.pack_cursor.saturating_sub(1);
                }
                KeyCode::Down if app.focus && matches!(active_tab, Tab::Select) && matches!(app.cmd, CmdKind::Pack) => {
                    if !app.pack_items.is_empty() {
                        app.pack_cursor = (app.pack_cursor + 1).min(app.pack_items.len() - 1);
                    }
                }
                KeyCode::Char(' ') if app.focus && matches!(active_tab, Tab::Select) && matches!(app.cmd, CmdKind::Pack) => {
                    toggle_pack_cursor(app);
                }
                KeyCode::Char('a') if app.focus && matches!(active_tab, Tab::Select) && matches!(app.cmd, CmdKind::Pack) => {
                    for v in &mut app.pack_selected {
                        *v = true;
                    }
                }
                KeyCode::Char('x') if app.focus && matches!(active_tab, Tab::Select) && matches!(app.cmd, CmdKind::Pack) => {
                    for v in &mut app.pack_selected {
                        *v = false;
                    }
                }

                // Focused Mode: Up/Down move, Space toggles current option.
                KeyCode::Up if app.focus && matches!(active_tab, Tab::Mode) => {
                    app.mode_cursor = app.mode_cursor.saturating_sub(1);
                }
                KeyCode::Down if app.focus && matches!(active_tab, Tab::Mode) => {
                    let max = mode_items_len(app);
                    if max > 0 {
                        app.mode_cursor = (app.mode_cursor + 1).min(max - 1);
                    }
                }
                KeyCode::Char(' ') if app.focus && matches!(active_tab, Tab::Mode) => {
                    toggle_mode_at_cursor(app);
                }

                // Focused Path: browse directories / choose dest or zip
                KeyCode::Up if app.focus && matches!(active_tab, Tab::Path) => {
                    app.path_cursor = app.path_cursor.saturating_sub(1);
                }
                KeyCode::Down if app.focus && matches!(active_tab, Tab::Path) => {
                    let len = app.path_entries.len();
                    if len > 0 {
                        app.path_cursor = (app.path_cursor + 1).min(len - 1);
                    }
                }
                KeyCode::Char(' ') if app.focus && matches!(active_tab, Tab::Path) => {
                    if let Some(ent) = app.path_entries.get(app.path_cursor).cloned() {
                        if ent.is_parent {
                            if let Some(parent) = app.path_current_dir.parent() {
                                app.path_current_dir = parent.to_path_buf();
                                refresh_path_entries(app)?;
                            }
                        } else if ent.is_dir {
                            let mut new_dir = app.path_current_dir.clone();
                            new_dir.push(&ent.name);
                            app.path_current_dir = new_dir;
                            refresh_path_entries(app)?;
                            if matches!(app.cmd, CmdKind::Pack) {
                                app.args.dest = Some(app.path_current_dir.clone());
                            }
                        } else if ent.is_zip && matches!(app.cmd, CmdKind::Unpack) {
                            let mut p = app.path_current_dir.clone();
                            p.push(&ent.name);
                            app.args.zip_path = Some(p);
                        }
                    }
                }

                // Run tab shortcuts
                KeyCode::Char('r') if !app.running && matches!(active_tab, Tab::Run) => {
                    run_command(app)?;
                }
                KeyCode::Char('p')
                    if !app.running && matches!(active_tab, Tab::Run) && matches!(app.cmd, CmdKind::Unpack) =>
                {
                    run_unpack_preview(app)?;
                    if let Some(pos) = tabs.iter().position(|t| matches!(t, Tab::Output)) {
                        app.tab_selected = pos;
                        app.tab_active = pos;
                        app.focus = false;
                    }
                }
                KeyCode::Char('c') if !app.running && matches!(active_tab, Tab::Run) && matches!(app.cmd, CmdKind::Pack) => {
                    run_pack_quick_check(app)?;
                    if let Some(pos) = tabs.iter().position(|t| matches!(t, Tab::Output)) {
                        app.tab_selected = pos;
                        app.tab_active = pos;
                        app.focus = false;
                    }
                }

                _ => {}
            }
        }
    }

    Ok(false)
}

fn handle_mouse(app: &mut App, m: MouseEvent, area: Rect) {
    match app.screen {
        Screen::Home => {
            if !matches!(m.kind, MouseEventKind::Down(_)) {
                return;
            }
            // area is the body (without header/footer)
            let click_y = m.row;
            let click_x = m.column;
            let _ = click_x;

            // List with borders + title: first item typically at area.y + 1
            let items_start_y = area.y.saturating_add(1);
            if click_y >= items_start_y && click_y < items_start_y.saturating_add(4) {
                let idx = (click_y - items_start_y) as usize;
                let idx = idx.min(3);
                let now = Instant::now();

                // double-click detection: same index within 400ms => enter Exec
                if let Some((last_t, last_idx)) = app.last_home_click {
                    if last_idx == idx && now.duration_since(last_t) <= Duration::from_millis(400) {
                        app.selected = idx;
                        // Same as keyboard Enter on Home
                        app.cmd = match app.selected {
                            0 => CmdKind::Info,
                            1 => CmdKind::Pack,
                            2 => CmdKind::Unpack,
                            _ => CmdKind::Check,
                        };
                        app.screen = Screen::Exec;
                        app.tab_selected = 0;
                        app.tab_active = 0;
                        app.focus = false;
                        app.mode_cursor = 0;
                        app.output.clear();
                        app.last_status = None;
                        if matches!(app.cmd, CmdKind::Pack) {
                            if let Err(e) = load_pack_list(app) {
                                app.output = format!("load pack list failed: {e:?}");
                            }
                        }
                        if matches!(app.cmd, CmdKind::Pack | CmdKind::Unpack) {
                            if let Err(e) = init_path_root(app) {
                                app.output = format!("init path picker failed: {e:?}");
                            }
                        }
                        app.last_home_click = None;
                        return;
                    }
                }

                // single click: only select item
                app.selected = idx;
                app.last_home_click = Some((now, idx));
            }
        }
        Screen::Exec => {
            let tabs = available_tabs(app);
            if tabs.is_empty() {
                return;
            }

            // Recompute same layout as draw_exec to avoid magic offsets
            let cols = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Length(22), Constraint::Min(0)])
                .split(area);
            let left = cols[0];
            let right = cols[1];
            let active_tab = tabs.get(app.tab_active).copied().unwrap_or(Tab::Run);

            match m.kind {
                MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
                    // Scroll wheel over right pane scrolls current list
                    if !point_in_rect(m.column, m.row, right) {
                        return;
                    }

                    let scroll_up = matches!(m.kind, MouseEventKind::ScrollUp);

                    match active_tab {
                        Tab::Select if matches!(app.cmd, CmdKind::Pack) => {
                            let len = app.pack_items.len();
                            if len == 0 {
                                return;
                            }
                            if scroll_up {
                                app.pack_cursor = app.pack_cursor.saturating_sub(1);
                            } else if app.pack_cursor + 1 < len {
                                app.pack_cursor += 1;
                            }
                        }
                        Tab::Mode => {
                            let len = mode_items_len(app);
                            if len == 0 {
                                return;
                            }
                            if scroll_up {
                                app.mode_cursor = app.mode_cursor.saturating_sub(1);
                            } else if app.mode_cursor + 1 < len {
                                app.mode_cursor += 1;
                            }
                        }
                        Tab::Path => {
                            let len = app.path_entries.len();
                            if len == 0 {
                                return;
                            }
                            if scroll_up {
                                app.path_cursor = app.path_cursor.saturating_sub(1);
                            } else if app.path_cursor + 1 < len {
                                app.path_cursor += 1;
                            }
                        }
                        _ => {}
                    }
                }
                MouseEventKind::Down(MouseButton::Left) => {
                    // Click on left tab bar selects + activates tab
                    if point_in_rect(m.column, m.row, left) {
                        // List inner area starts at y = left.y + 1 (border)
                        let inner_y0 = left.y.saturating_add(1);
                        if m.row >= inner_y0 {
                            let idx = (m.row - inner_y0) as usize;
                            if idx < tabs.len() {
                                app.tab_selected = idx;
                                app.tab_active = idx;
                            }
                        }
                        return;
                    }

                    // Click on progress gauge in Run tab jumps to Output
                    if matches!(active_tab, Tab::Run) {
                        // Follow same structure as draw_run: split right into left(40) + gauge+output
                        let h = Layout::default()
                            .direction(Direction::Horizontal)
                            .constraints([Constraint::Length(40), Constraint::Min(0)])
                            .split(right);
                        let gauge_and_out = h[1];
                        let v = Layout::default()
                            .direction(Direction::Vertical)
                            .constraints([Constraint::Length(3), Constraint::Min(0)])
                            .split(gauge_and_out);
                        let gauge_rect = v[0];

                        if point_in_rect(m.column, m.row, gauge_rect) {
                            if let Some(pos) = tabs.iter().position(|t| matches!(t, Tab::Output)) {
                                app.tab_selected = pos;
                                app.tab_active = pos;
                            }
                            return;
                        }
                    }

                    // Click inside right pane on Select/Mode/Path lists to move cursor (and for Path, activate entry)
                    if !point_in_rect(m.column, m.row, right) {
                        return;
                    }

                    match active_tab {
                        Tab::Select if matches!(app.cmd, CmdKind::Pack) => {
                            let total = app.pack_items.len();
                            if total == 0 {
                                return;
                            }
                            let inner_y0 = right.y.saturating_add(1); // border
                            if m.row < inner_y0 {
                                return;
                            }
                            let row_off = (m.row - inner_y0) as usize;
                            let capacity = right.height.saturating_sub(2).max(1) as usize;
                            let cursor = app.pack_cursor.min(total.saturating_sub(1));
                            let start = if total <= capacity {
                                0
                            } else if cursor < capacity {
                                0
                            } else if cursor >= total - capacity {
                                total - capacity
                            } else {
                                cursor + 1 - capacity
                            };
                            let idx = start.saturating_add(row_off);
                            if idx < total {
                                app.pack_cursor = idx;
                                app.focus = true;
                                // Single click toggles selection, same as pressing Space
                                toggle_pack_cursor(app);
                            }
                        }
                        Tab::Mode => {
                            let total = mode_items_len(app);
                            if total == 0 {
                                return;
                            }
                            let inner_y0 = right.y.saturating_add(1); // border
                            if m.row < inner_y0 {
                                return;
                            }
                            let row_off = (m.row - inner_y0) as usize;
                            if row_off < total {
                                app.mode_cursor = row_off;
                                app.focus = true;
                                // Single click toggles option, same as pressing Space
                                toggle_mode_at_cursor(app);
                            }
                        }
                        Tab::Path => {
                            let total = app.path_entries.len();
                            if total == 0 {
                                return;
                            }
                            // borders + 2 header lines (cwd + blank)
                            let inner_y0 = right.y.saturating_add(3);
                            if m.row < inner_y0 {
                                return;
                            }
                            let row_off = (m.row - inner_y0) as usize;
                            let capacity = right.height.saturating_sub(4).max(1) as usize;
                            let cursor = app.path_cursor.min(total.saturating_sub(1));
                            let start = if total <= capacity {
                                0
                            } else if cursor < capacity {
                                0
                            } else if cursor >= total - capacity {
                                total - capacity
                            } else {
                                cursor + 1 - capacity
                            };
                            let idx = start.saturating_add(row_off);
                            if idx >= total {
                                return;
                            }
                            app.path_cursor = idx;
                            app.focus = true;

                            // Activate entry like Space: go into dir or select zip/dest
                            if let Some(ent) = app.path_entries.get(idx).cloned() {
                                if ent.is_parent {
                                    if let Some(parent) = app.path_current_dir.parent() {
                                        app.path_current_dir = parent.to_path_buf();
                                        let _ = refresh_path_entries(app);
                                    }
                                } else if ent.is_dir {
                                    let mut new_dir = app.path_current_dir.clone();
                                    new_dir.push(&ent.name);
                                    app.path_current_dir = new_dir;
                                    let _ = refresh_path_entries(app);
                                    if matches!(app.cmd, CmdKind::Pack) {
                                        app.args.dest = Some(app.path_current_dir.clone());
                                    }
                                } else if ent.is_zip && matches!(app.cmd, CmdKind::Unpack) {
                                    let mut p = app.path_current_dir.clone();
                                    p.push(&ent.name);
                                    app.args.zip_path = Some(p);
                                }
                            }
                        }
                        _ => {}
                    }
                }
                _ => {}
            }
        }
    }
}

fn point_in_rect(x: u16, y: u16, r: Rect) -> bool {
    x >= r.x && x < r.x.saturating_add(r.width) && y >= r.y && y < r.y.saturating_add(r.height)
}

fn run_command(app: &mut App) -> Result<()> {
    let exe = std::env::current_exe().context("current_exe")?;
    let mut args: Vec<String> = Vec::new();

    match app.cmd {
        CmdKind::Info => {
            args.push("info".to_string());
        }
        CmdKind::Pack => {
            args.push("pack".to_string());
            // Pass selected plugin ids as positional args. If none selected, pack all.
            let selected = selected_pack_ids(app);
            for id in selected {
                args.push(id);
            }

            if app.args.no_md5 {
                args.push("--no-md5".to_string());
            }
        }
        CmdKind::Unpack => {
            args.push("unpack".to_string());
            let zip = app
                .args
                .zip_path
                .clone()
                .unwrap_or_else(|| PathBuf::from("neko_plugins_bundle.zip"));
            args.push(zip.to_string_lossy().to_string());
            if app.args.force {
                args.push("--force".to_string());
            }
        }
        CmdKind::Check => {
            args.push("check".to_string());
            if let Some(pid) = &app.args.plugin_id {
                if !pid.trim().is_empty() {
                    args.push(pid.clone());
                }
            }
            args.push("--json".to_string());
            if app.args.python {
                args.push("--python".to_string());
            }
            if app.args.python_strict {
                args.push("--python-strict".to_string());
            }
        }
    }

    if let Some(root) = &app.args.root {
        args.push("--root".to_string());
        args.push(root.to_string_lossy().to_string());
    }

    match app.cmd {
        // For Pack, interpret dest as an output directory and map it to --out <dir>/neko_plugins_bundle.zip
        CmdKind::Pack => {
            if let Some(dest_dir) = &app.args.dest {
                let mut out_path = dest_dir.clone();
                out_path.push("neko_plugins_bundle.zip");
                args.push("--out".to_string());
                args.push(out_path.to_string_lossy().to_string());
            }
        }
        // For Unpack, dest is the destination plugin directory and maps directly to --dest
        CmdKind::Unpack => {
            if let Some(dest) = &app.args.dest {
                args.push("--dest".to_string());
                args.push(dest.to_string_lossy().to_string());
            }
        }
        _ => {}
    }

    app.running = true;
    app.started_at = Some(Instant::now());
    app.output.clear();
    app.last_status = None;

    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let out = Command::new(exe)
            .args(&args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .output();
        let _ = tx.send(out.map_err(|e| anyhow::anyhow!(e)));
    });

    app.task_rx = Some(rx);

    Ok(())
}

fn run_pack_quick_check(app: &mut App) -> Result<()> {
    let exe = std::env::current_exe().context("current_exe")?;
    let repo_root = if let Some(r) = &app.args.root {
        r.clone()
    } else {
        core::find_repo_root(std::env::current_dir().context("failed to get cwd")?)?
    };

    let selected = selected_pack_ids(app);
    if selected.is_empty() {
        app.output = "No plugin selected (treat as all). Quick check requires explicit selection.\n".to_string();
        app.last_status = Some(0);
        return Ok(());
    }

    let mut out_all = String::new();
    for id in selected {
        let output = Command::new(&exe)
            .arg("check")
            .arg(id.clone())
            .arg("--root")
            .arg(repo_root.to_string_lossy().to_string())
            .arg("--json")
            .output()
            .with_context(|| format!("failed to run check for {id}"))?;
        out_all.push_str(&format!("=== check {id} (exit={}) ===\n", output.status.code().unwrap_or(-1)));
        out_all.push_str(&String::from_utf8_lossy(&output.stdout));
        if !output.stderr.is_empty() {
            if !out_all.ends_with('\n') {
                out_all.push('\n');
            }
            out_all.push_str(&String::from_utf8_lossy(&output.stderr));
        }
        if !out_all.ends_with('\n') {
            out_all.push('\n');
        }
    }

    app.output = out_all;
    app.last_status = Some(0);
    Ok(())
}

fn run_unpack_preview(app: &mut App) -> Result<()> {
    use crate::core;

    let repo_root = if let Some(r) = &app.args.root {
        r.clone()
    } else {
        core::find_repo_root(std::env::current_dir().context("failed to get cwd")?)?
    };

    let dest_dir = app
        .args
        .dest
        .clone()
        .unwrap_or_else(|| repo_root.join("plugin").join("plugins"));

    let zip_path = app
        .args
        .zip_path
        .clone()
        .unwrap_or_else(|| PathBuf::from("neko_plugins_bundle.zip"));

    let excludes = core::build_excludes(&[])?;
    let preview_items = core::preview_unpack(&zip_path, &dest_dir, app.args.force, &excludes)?;

    let mut out = String::new();
    use std::fmt::Write as _;
    writeln!(
        &mut out,
        "Unpack preview for {}\nDest: {}  force={}\n",
        zip_path.display(),
        dest_dir.display(),
        app.args.force
    )
    .ok();

    if preview_items.is_empty() {
        writeln!(&mut out, "(manifest has no plugins)").ok();
    } else {
        for item in preview_items {
            let action = if item.will_install { "INSTALL" } else { "SKIP" };
            writeln!(
                &mut out,
                "- [{}] id={} folder={}\n    {}",
                action, item.id, item.folder, item.reason
            )
            .ok();
        }
    }

    app.output = out;
    app.last_status = Some(0);
    Ok(())
}

fn copy_output_to_clipboard(app: &mut App) {
    if app.output.is_empty() {
        return;
    }
    if let Some(cb) = &mut app.clipboard {
        let _ = cb.set_text(app.output.clone());
    }
}

fn draw(f: &mut Frame<'_>, app: &App) {
    let size = f.area();
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(0),
            Constraint::Length(3),
        ])
        .split(size);

    let title = match app.screen {
        Screen::Home => "neko_plugin_cli TUI - Home",
        Screen::Exec => "neko_plugin_cli TUI - Exec",
    };

    let help_hint = if app.show_help { " [q: close help]" } else { " (q: help)" };

    let header = Paragraph::new(Line::from(vec![
        Span::styled("N.E.K.O ", Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
        Span::raw("/ "),
        Span::raw(format!("{}{}", title, help_hint)),
    ]))
    .block(Block::default().borders(Borders::ALL));
    f.render_widget(header, chunks[0]);

    if app.show_help {
        draw_help(f, chunks[1]);
        // Minimal footer when showing help
        let footer = Paragraph::new("Press q or Esc to close help / 按 q 或 Esc 关闭帮助")
            .wrap(Wrap { trim: true })
            .block(Block::default().borders(Borders::ALL));
        f.render_widget(footer, chunks[2]);
    } else {
        match app.screen {
            Screen::Home => draw_home(f, app, chunks[1]),
            Screen::Exec => draw_exec(f, app, chunks[1]),
        }

        // Default: footer without verbose shortcut hints (empty box)
        let footer = Paragraph::new("")
            .wrap(Wrap { trim: true })
            .block(Block::default().borders(Borders::ALL));
        f.render_widget(footer, chunks[2]);
    }
}

fn draw_help(f: &mut Frame<'_>, area: Rect) {
    let lines = vec![
        Line::from(Span::styled(
            "Keyboard shortcuts / 键盘快捷键",
            Style::default().add_modifier(Modifier::BOLD),
        )),
        Line::from("(按 q 或 Esc 退出帮助 / Press q or Esc to close)"),
        Line::from(""),
        Line::from(Span::styled("Global / 全局", Style::default().add_modifier(Modifier::BOLD))),
        Line::from("  Ctrl-C×2 / Ctrl-Q×2  退出 TUI / Exit TUI"),
        Line::from("  Esc: 从 Exec 返回 Home / back to Home from Exec"),
        Line::from(""),
        Line::from(Span::styled("Home", Style::default().add_modifier(Modifier::BOLD))),
        Line::from("  ↑↓: 选择命令 / select command"),
        Line::from("  Enter: 进入 Exec / enter Exec screen"),
        Line::from("  鼠标双击: 进入 Exec / mouse double-click to enter Exec"),
        Line::from(""),
        Line::from(Span::styled("Exec / 执行界面", Style::default().add_modifier(Modifier::BOLD))),
        Line::from("  左侧 Tab: ↑↓ 切换 / change tab; Enter/→ 聚焦, ← 取消聚焦"),
        Line::from(""),
        Line::from(Span::styled("Pack", Style::default().add_modifier(Modifier::BOLD))),
        Line::from("  Select: ↑↓ 移动, Space 选中/取消, a 全选, x 全不选"),
        Line::from("  Mode: ↑↓ 移动, Space 切换 no_md5"),
        Line::from("  Path: ↑↓ 目录移动, Space 进入目录并设置输出目录"),
        Line::from("  Run: r 执行 pack, c 对选中插件 quick check"),
        Line::from(""),
        Line::from(Span::styled("Unpack", Style::default().add_modifier(Modifier::BOLD))),
        Line::from("  Mode: Space 切换 force"),
        Line::from("  Path: ↑↓ 目录/zip 移动, Space 选择 .zip"),
        Line::from("  Run: r 执行 unpack, p 预览将安装/跳过哪些插件"),
        Line::from(""),
        Line::from(Span::styled("Check / Info", Style::default().add_modifier(Modifier::BOLD))),
        Line::from("  Mode: ↑↓/Space 切换 python / python_strict 等选项"),
        Line::from("  Run: r 运行 info/check"),
        Line::from(""),
        Line::from(Span::styled("Output", Style::default().add_modifier(Modifier::BOLD))),
        Line::from("  Ctrl-Y / Ctrl-Insert: 复制输出到剪贴板 / copy output to clipboard"),
        Line::from(""),
        Line::from("鼠标: Home 双击命令进入 Exec；Exec 左侧点击切换 Tab；Run 进度条区域点击跳转到 Output"),
    ];

    let p = Paragraph::new(lines)
        .block(Block::default().borders(Borders::ALL).title("Help / 帮助"))
        .wrap(Wrap { trim: true });
    f.render_widget(p, area);
}

fn draw_home(f: &mut Frame<'_>, app: &App, area: Rect) {
    let items = [
        "Info  显示概览 / Show summary",
        "Pack  打包插件 / Pack plugins",
        "Unpack  解包插件 / Unpack plugins",
        "Check  检查插件 / Check plugins",
    ]
    .iter()
    .enumerate()
    .map(|(i, s)| {
        let style = if i == app.selected {
            Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)
        } else {
            Style::default()
        };
        ListItem::new(Line::from(Span::styled(*s, style)))
    })
    .collect::<Vec<_>>();

    let list = List::new(items)
        .block(Block::default().borders(Borders::ALL).title("Commands / 命令"));
    f.render_widget(list, area);
}

fn draw_run(f: &mut Frame<'_>, app: &App, area: Rect, highlight: bool) {
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(40), Constraint::Min(0)])
        .split(area);

    let left = cols[0];
    let right = cols[1];

    let mut lines: Vec<Line> = Vec::new();
    lines.push(Line::from(vec![Span::styled(
        format!("Command: {}", app.cmd.title()),
        Style::default().add_modifier(Modifier::BOLD),
    )]));
    if let Some(root) = &app.args.root {
        lines.push(Line::from(format!("--root {}", root.display())));
    } else {
        lines.push(Line::from("--root <auto>"));
    }
    if matches!(app.cmd, CmdKind::Pack) {
        let selected_count = app.pack_selected.iter().filter(|x| **x).count();
        lines.push(Line::from(format!(
            "selected: {} / {} (c: quick check)",
            selected_count,
            app.pack_items.len()
        )));
        lines.push(Line::from(format!("no_md5: {} (set in Mode)", app.args.no_md5)));
    }
    if matches!(app.cmd, CmdKind::Unpack) {
        if let Some(zip) = &app.args.zip_path {
            lines.push(Line::from(format!("zip: {}", zip.display())));
        } else {
            lines.push(Line::from("zip: <在 Path 中选择 .zip>"));
        }
        lines.push(Line::from(format!("force: {} (set in Mode)", app.args.force)));
    }
    if matches!(app.cmd, CmdKind::Check) {
        lines.push(Line::from(format!("python: {} (set in Mode)", app.args.python)));
        lines.push(Line::from(format!(
            "python_strict: {} (set in Mode)",
            app.args.python_strict
        )));
    }
    let left_panel = Paragraph::new(lines)
        .block(
            Block::default()
                .borders(Borders::ALL)
                .title("Run / 执行"),
        )
        .wrap(Wrap { trim: true });
    f.render_widget(left_panel, left);

    let prog = if app.running { 0.5 } else { 1.0 };
    let spinner = ["-", "\\", "|", "/"][app.spinner_i % 4];
    let status_line = if app.running {
        let elapsed = app.started_at.map(|t| t.elapsed()).unwrap_or_default();
        format!("Running {spinner}  elapsed: {:.1}s", elapsed.as_secs_f64())
    } else {
        match app.last_status {
            Some(0) => "Done (exit=0)".to_string(),
            Some(c) => format!("Done (exit={c})"),
            None => "Idle (press 'r' to run)".to_string(),
        }
    };

    let right_chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(3), Constraint::Min(0)])
        .split(right);

    let gauge_border = if highlight {
        Style::default().fg(Color::Green)
    } else {
        Style::default()
    };
    let gauge = Gauge::default()
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(gauge_border)
                .title("Progress / 进度"),
        )
        .gauge_style(Style::default().fg(Color::Green))
        .label(status_line)
        .ratio(prog);
    f.render_widget(gauge, right_chunks[0]);

    let out_border = if highlight {
        Style::default().fg(Color::Green)
    } else {
        Style::default()
    };
    let out = Paragraph::new(app.output.clone())
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(out_border)
                .title("Output / 输出（preview）"),
        )
        .wrap(Wrap { trim: false });
    f.render_widget(out, right_chunks[1]);
}

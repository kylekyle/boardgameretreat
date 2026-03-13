import time
import streamlit as st
from streamlit_javascript import st_javascript
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="Board Game Retreat", page_icon="🎲", layout="centered")

# ── Google Sheets setup ───────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

@st.cache_resource
def get_sheet():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    client = gspread.authorize(creds)
    return client.open(st.secrets["sheets"]["spreadsheet_name"])

def load_games():
    sheet = get_sheet().worksheet("games")
    records = sheet.get_all_records()
    return pd.DataFrame(records) if records else pd.DataFrame(
        columns=["id", "title", "host", "max_players", "players", "status", "notes"]
    )

def save_game(title, host, max_players, notes):
    sheet = get_sheet().worksheet("games")
    game_id = str(int(time.time()))
    sheet.append_row([game_id, title, host, max_players, host, "open", notes])

def join_game(game_id, player_name):
    sheet = get_sheet().worksheet("games")
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):  # row 1 is header
        if str(row["id"]) == str(game_id):
            current = row["players"]
            if player_name not in current.split(", "):
                updated = f"{current}, {player_name}" if current else player_name
                sheet.update_cell(i, 6, updated)  # col 6 = players
            break

def leave_game(game_id, player_name):
    sheet = get_sheet().worksheet("games")
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):
        if str(row["id"]) == str(game_id):
            players = [p.strip() for p in row["players"].split(",") if p.strip() != player_name]
            sheet.update_cell(i, 6, ", ".join(players))
            break

def close_game(game_id):
    sheet = get_sheet().worksheet("games")
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):
        if str(row["id"]) == str(game_id):
            sheet.update_cell(i, 7, "closed")  # col 7 = status
            break

# ── Session persistence via localStorage ─────────────────────────────────────
def load_user_from_storage():
    """Read the saved username from browser localStorage."""
    return st_javascript("localStorage.getItem('retreat_user') || ''")

def save_user_to_storage(name: str):
    """Persist the username to browser localStorage."""
    safe = name.replace("'", "\\'")
    st_javascript(f"localStorage.setItem('retreat_user', '{safe}')")

def clear_user_from_storage():
    st_javascript("localStorage.removeItem('retreat_user')")

# ── Identity sidebar ──────────────────────────────────────────────────────────
def identity_sidebar():
    with st.sidebar:
        st.title("🎲 Board Game Retreat")
        st.divider()

        # Only read localStorage once per session to avoid flicker
        if "user_loaded" not in st.session_state:
            stored = load_user_from_storage()
            # st_javascript returns 0 on first render; wait for real value
            if isinstance(stored, str) and stored:
                st.session_state["player_name"] = stored
            st.session_state["user_loaded"] = True

        if "player_name" not in st.session_state:
            st.session_state["player_name"] = ""

        name = st.text_input("Your name", value=st.session_state["player_name"], key="name_input")

        if st.button("Save name", use_container_width=True):
            if name.strip():
                st.session_state["player_name"] = name.strip()
                save_user_to_storage(name.strip())
                st.success("Saved!")
            else:
                st.warning("Please enter a name.")

        if st.session_state["player_name"]:
            st.caption(f"Playing as **{st.session_state['player_name']}**")
            if st.button("Clear", use_container_width=True):
                st.session_state["player_name"] = ""
                clear_user_from_storage()
                st.rerun()

        st.divider()
        st.caption("Data refreshes every 30s automatically.")

# ── Game list (auto-refreshing fragment) ──────────────────────────────────────
@st.fragment(run_every=30)
def game_list():
    player = st.session_state.get("player_name", "")

    col1, col2 = st.columns([3, 1])
    with col1:
        st.subheader("Open Games")
    with col2:
        if st.button("Refresh", use_container_width=True):
            st.rerun(scope="fragment")

    try:
        df = load_games()
    except Exception as e:
        st.error(f"Could not load games: {e}")
        return

    open_games = df[df["status"] == "open"] if not df.empty else df

    if open_games.empty:
        st.info("No games open yet. Be the first to host one!")
    else:
        for _, game in open_games.iterrows():
            players_list = [p.strip() for p in str(game["players"]).split(",") if p.strip()]
            spots_left = int(game["max_players"]) - len(players_list)
            is_joined = player in players_list
            is_host = player == game["host"]

            with st.container(border=True):
                c1, c2 = st.columns([4, 1])
                with c1:
                    st.markdown(f"**{game['title']}**  •  hosted by *{game['host']}*")
                    st.caption(
                        f"Players ({len(players_list)}/{game['max_players']}): "
                        + (", ".join(players_list) if players_list else "—")
                    )
                    if game["notes"]:
                        st.caption(f"📝 {game['notes']}")
                with c2:
                    if not player:
                        st.caption("Set your name to join")
                    elif is_host:
                        if st.button("Close", key=f"close_{game['id']}", use_container_width=True):
                            close_game(game["id"])
                            st.rerun(scope="fragment")
                    elif is_joined:
                        if st.button("Leave", key=f"leave_{game['id']}", use_container_width=True):
                            leave_game(game["id"], player)
                            st.rerun(scope="fragment")
                    elif spots_left > 0:
                        if st.button("Join", key=f"join_{game['id']}", use_container_width=True):
                            join_game(game["id"], player)
                            st.rerun(scope="fragment")
                    else:
                        st.caption("Full")

    # Closed games (collapsed)
    closed_games = df[df["status"] == "closed"] if not df.empty else pd.DataFrame()
    if not closed_games.empty:
        with st.expander(f"Closed games ({len(closed_games)})"):
            for _, game in closed_games.iterrows():
                st.markdown(f"~~{game['title']}~~ — *{game['host']}*")

# ── Host a game form ──────────────────────────────────────────────────────────
def host_game_form():
    player = st.session_state.get("player_name", "")
    st.subheader("Host a Game")

    if not player:
        st.warning("Set your name in the sidebar before hosting a game.")
        return

    with st.form("host_form", clear_on_submit=True):
        title = st.text_input("Game title")
        max_players = st.number_input("Max players", min_value=2, max_value=20, value=4)
        notes = st.text_input("Notes (optional)", placeholder="e.g. beginner friendly, ~2hrs")
        submitted = st.form_submit_button("Post game", use_container_width=True)

    if submitted:
        if not title.strip():
            st.error("Please enter a game title.")
        else:
            save_game(title.strip(), player, int(max_players), notes.strip())
            st.success(f"Posted **{title}**! It will appear in the list shortly.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    identity_sidebar()

    tab_games, tab_host = st.tabs(["Games", "Host a Game"])

    with tab_games:
        game_list()

    with tab_host:
        host_game_form()

if __name__ == "__main__":
    main()

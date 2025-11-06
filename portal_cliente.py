import streamlit as st
import pandas as pd
import gspread
import time
import os
import warnings
import io

# --- INICIO: IMPORTS PARA GOOGLE (OAuth) ---
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
# --- FIN: IMPORTS PARA GOOGLE ---

# Ignorar advertencias comunes
warnings.filterwarnings('ignore', category=FutureWarning)

# ========== CONFIGURACIÓN ==========
class Config:
    # 1. El JSON que descargaste de "ID de cliente de OAuth"
    GDRIVE_CREDENTIALS_FILE = "client_secrets.json" # <-- REEMPLAZA ESTO
    
    # 2. El token que creaste con auth.py
    GDRIVE_TOKEN_FILE = "token.json" # <-- DEBE COINCIDIR CON EL CREADO
    
    # 3. El nombre EXACTO de tu Google Sheet
    GSHEET_NAME = "BaseDeDatos_TalentHub" # <-- REEMPLAZA ESTO
    
    # 4. ID de la carpeta donde se guardarán las entrevistas (OPCIONAL)
    ENTREVISTAS_FOLDER_ID = None  # <-- Si usas una carpeta específica, pon su ID aquí
    
# Definimos los "permisos" (necesitamos leer y escribir)
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

# Definimos las columnas que esperamos leer
COLUMN_HEADERS = ["Archivo", "Clasificación", "Comentarios", "Fecha", "Proceso", "CV_Link", "Estado_Pipeline", "Entrevistas"]

# --- ¡NUEVO! DEFINICIÓN DEL PIPELINE ---
# Estas son las FASES de tu tablero
PIPELINE_STAGES = [
    "📥 Nuevo",
    "👀 En Revisión",
    "🗓️ Agendar Entrevista",
    "🎤 Entrevistado",
    "✅ Aceptado"
]
RECHAZADO_STAGE = "❌ Rechazado"

# Fases donde se permiten subir entrevistas
FASES_ENTREVISTA = ["🗓️ Agendar Entrevista", "🎤 Entrevistado", "✅ Aceptado"]

# Traemos tus categorías para usar los colores
CATEGORIES = [
    {"label": "🌟 Óptimo", "color": "#D4ADFC"},
    {"label": "✅ Adecuado", "color": "#A0E7E5"},
]

# ========== CONEXIÓN A GOOGLE SHEETS (OAuth) ==========

@st.cache_resource(ttl=600)
def get_google_creds(token_file):
    """Lee el token.json. Si no existe, falla."""
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    else:
        st.error(f"Error: No se encuentra el archivo de sesión '{token_file}'.")
        st.info("Por favor, ejecuta 'python auth.py' en tu terminal primero para autenticarte.")
        return None
    
    # Refrescar si está caducado
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_file, 'w') as token:
                token.write(creds.to_json())
        except Exception as e:
            st.error(f"Error al refrescar el token: {e}. Ejecuta 'python auth.py' de nuevo.")
            return None
    
    return creds

@st.cache_resource(ttl=600)
def connect_to_gsheet(_creds):
    """Conecta con Google Sheets."""
    if _creds is None: return None
    try:
        gc = gspread.authorize(_creds)
        sh = gc.open(Config.GSHEET_NAME)
        worksheet = sh.sheet1
        return worksheet
    except Exception as e:
        st.error(f"Error conectando con Google Sheets: {e}")
        return None

@st.cache_data(ttl=60) # Cachear los datos por 60 segundos
def load_data_from_gsheet(_worksheet):
    """Lee TODOS los datos de Google Sheets y los devuelve como DataFrame."""
    try:
        data = _worksheet.get_all_records()
        df = pd.DataFrame(data)
        
        if df.empty:
            return pd.DataFrame(columns=COLUMN_HEADERS)
        
        for col in COLUMN_HEADERS:
            if col not in df.columns:
                df[col] = pd.NA
                
        return df[COLUMN_HEADERS]
        
    except Exception as e:
        st.error(f"Error leyendo el DataFrame de Google Sheets: {e}")
        return pd.DataFrame(columns=COLUMN_HEADERS)

# --- LÓGICA DE ESCRITURA (MOVER CANDIDATO) ---
def mover_candidato(candidato_archivo, nuevo_estado):
    """Encuentra al candidato en el GSheet y actualiza su estado."""
    try:
        # Volver a conectar (no se puede pasar 'worksheet' como arg a on_click)
        creds = get_google_creds(Config.GDRIVE_TOKEN_FILE)
        worksheet = connect_to_gsheet(creds)
        if worksheet is None:
            st.error("Error de conexión al mover candidato.")
            return

        # 1. Encontrar al candidato por su nombre de archivo
        cell = worksheet.find(candidato_archivo)
        if not cell:
            st.error(f"Error: No se encontró a '{candidato_archivo}' para moverlo.")
            return
            
        # 2. Encontrar la columna "Estado_Pipeline"
        headers = worksheet.row_values(1)
        try:
            col_index = headers.index("Estado_Pipeline") + 1 # +1 porque gspread empieza en 1
        except ValueError:
            st.error("Error: No se encontró la columna 'Estado_Pipeline' en el Google Sheet.")
            return
            
        # 3. Actualizar la celda
        worksheet.update_cell(cell.row, col_index, nuevo_estado)
        
        # 4. Limpiar caché
        st.cache_data.clear() # Borra la caché de datos para forzar la recarga
        st.success(f"Movido '{candidato_archivo}' a '{nuevo_estado}'!")
        # Streamlit recarga automáticamente después de un callback

    except Exception as e:
        st.error(f"Error al mover candidato: {e}")

# --- NUEVA FUNCIÓN: SUBIR ENTREVISTA ---
def subir_entrevista(candidato_archivo, archivo_subido):
    """Sube un informe de entrevista y lo asocia al candidato"""
    try:
        creds = get_google_creds(Config.GDRIVE_TOKEN_FILE)
        
        # Conectar a Google Drive
        drive_service = build('drive', 'v3', credentials=creds)
        
        # Preparar metadatos del archivo
        file_metadata = {
            'name': f"Entrevista_{candidato_archivo}_{archivo_subido.name}",
            'mimeType': 'application/pdf'
        }
        
        # Si se especificó una carpeta, usarla
        if Config.ENTREVISTAS_FOLDER_ID:
            file_metadata['parents'] = [Config.ENTREVISTAS_FOLDER_ID]
        
        # Convertir el archivo subido a bytes
        file_bytes = archivo_subido.getvalue()
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), 
                                mimetype='application/pdf',
                                resumable=True)
        
        # Subir archivo a Google Drive
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink, name'
        ).execute()
        
        # Actualizar Google Sheets con el nuevo enlace
        worksheet = connect_to_gsheet(creds)
        cell = worksheet.find(candidato_archivo)
        if cell:
            # Encontrar columna de Entrevistas
            headers = worksheet.row_values(1)
            try:
                col_index = headers.index("Entrevistas") + 1
            except ValueError:
                # Si la columna no existe, crearla
                col_index = len(headers) + 1
                worksheet.update_cell(1, col_index, "Entrevistas")
            
            # Obtener entrevistas existentes
            entrevistas_actuales = worksheet.cell(cell.row, col_index).value or ""
            nueva_entrevista = f"{file['webViewLink']}|{archivo_subido.name}"
            
            # Agregar nueva entrevista (separadas por ;)
            if entrevistas_actuales:
                entrevistas_actuales += f";{nueva_entrevista}"
            else:
                entrevistas_actuales = nueva_entrevista
                
            worksheet.update_cell(cell.row, col_index, entrevistas_actuales)
            
        st.success(f"✅ Entrevista subida para {candidato_archivo}")
        st.cache_data.clear()  # Forzar recarga de datos
        
    except Exception as e:
        st.error(f"❌ Error subiendo entrevista: {e}")

# ========== INTERFAZ "MODO CARRERA" ==========

def mostrar_ficha_candidato(candidato_row, current_stage_index):
    """
    Dibuja la "Ficha de Jugador" para un candidato, CON BOTONES DE ACCIÓN.
    """
    
    # 1. Obtener los datos de la fila
    nombre = candidato_row.get("Archivo", "Sin Nombre")
    clasificacion = candidato_row.get("Clasificación", "")
    comentarios = candidato_row.get("Comentarios", "")
    link_cv = candidato_row.get("CV_Link", "#")
    entrevistas = candidato_row.get("Entrevistas", "")
    
    # 2. Obtener el color de tu clasificación personal
    color = "#CCCCCC" # Color por defecto
    if clasificacion == "🌟 Óptimo":
        color = CATEGORIES[0]['color']
    elif clasificacion == "✅ Adecuado":
        color = CATEGORIES[1]['color']
    
    # 3. Dibujar la tarjeta CON COMPONENTES NATIVOS DE STREAMLIT
    with st.container(border=True):
        
        # Cabecera: Nombre y Clasificación
        col_header = st.columns([4, 1])
        with col_header[0]:
            st.markdown(f"**{nombre}**")
        with col_header[1]:
            st.markdown(
                f'<span style="background-color: {color}; color: #333; padding: 4px 10px; border-radius: 15px; font-size: 0.8rem; font-weight: bold;">{clasificacion}</span>',
                unsafe_allow_html=True
            )
        
        # Comentarios (solo si existen)
        if comentarios and pd.notna(comentarios) and str(comentarios).strip():
            st.markdown(f'*"{comentarios}"*')
        
        # Botón de Ver CV
        if link_cv and pd.notna(link_cv) and link_cv != "#":
            st.link_button("📄 Ver CV", link_cv)
        
        # --- NUEVO: MOSTRAR ENTREVISTAS EXISTENTES ---
        # Manejo seguro de valores NA/nulos de pandas
        if pd.notna(entrevistas) and entrevistas and str(entrevistas).strip():
            st.markdown("---")
            st.markdown("**📋 Informes de Entrevista:**")
            entrevistas_lista = [e for e in str(entrevistas).split(";") if e.strip()]
            for i, entrevista in enumerate(entrevistas_lista):
                if "|" in entrevista:
                    link, nombre_archivo = entrevista.split("|", 1)
                    st.markdown(f"• [{nombre_archivo}]({link})")
        
        # --- NUEVO: SUBIR NUEVA ENTREVISTA (solo en fases de entrevista) ---
        if st.session_state.selected_phase in FASES_ENTREVISTA:
            st.markdown("---")
            st.markdown("**Subir nuevo informe de entrevista:**")
            
            # Usamos un form para evitar reruns automáticos
            with st.form(key=f"form_entrevista_{nombre}"):
                archivo_entrevista = st.file_uploader(
                    "Seleccionar PDF de entrevista",
                    type=["pdf"],
                    key=f"entrevista_{nombre}"
                )
                
                submit_button = st.form_submit_button(
                    "📤 Subir Entrevista",
                    type="secondary",
                    use_container_width=True
                )
                
                if submit_button and archivo_entrevista is not None:
                    with st.spinner("Subiendo entrevista..."):
                        subir_entrevista(nombre, archivo_entrevista)
                        st.rerun()
                elif submit_button and archivo_entrevista is None:
                    st.warning("Por favor, selecciona un archivo PDF primero.")
        
        # --- Botones de Acción ---
        col1, col2 = st.columns([3, 1])

        with col1:
            # Botón para mover al SIGUIENTE estado
            if current_stage_index < len(PIPELINE_STAGES) - 1:
                next_stage = PIPELINE_STAGES[current_stage_index + 1]
                st.button(
                    f"Mover a {next_stage}", 
                    key=f"move_{nombre}_{next_stage}",
                    on_click=mover_candidato,
                    args=(nombre, next_stage),
                    use_container_width=True,
                    type="primary"
                )
        
        with col2:
            # Botón para RECHAZAR
            st.button(
                "❌", 
                key=f"reject_{nombre}",
                on_click=mover_candidato,
                args=(nombre, RECHAZADO_STAGE),
                use_container_width=True,
                help="Mover a 'Rechazado'",
                type="secondary"
            )

# (Traemos el CSS de la otra app para un look coherente)
def setup_portal_design():
    st.markdown("""
    <style>
    /* Fondo con gradiente pastel suave */
    .stApp {
        background: linear-gradient(135deg, #E0F7FA 0%, #FCE4EC 100%);
        color: #333333;
    }
    
    /* Títulos con gradiente pastel */
    .main-title {
        font-size: 3rem !important;
        font-weight: 800 !important;
        text-align: center;
        margin-bottom: 0.5rem !important;
        background: linear-gradient(45deg, #81D4FA, #CE93D8);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    
    /* Botón principal de mover */
    button[type="primary"] {
        background: linear-gradient(45deg, #81D4FA, #CE93D8) !important;
        color: white !important;
        border: none !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        height: 40px; /* Altura fija */
    }
    
    /* Botón secundario de rechazar */
    button[type="secondary"] {
        background-color: #FFB3BA !important;
        color: #333 !important;
        border: none !important;
        border-radius: 8px !important;
        height: 40px; /* Altura fija */
    }

    /* Estilo para los botones de navegación del Sidebar */
    .stSidebar .stButton button {
        background-color: #FFFFFF99;
        border: 2px solid #81D4FA;
        color: #333;
        font-weight: 600;
        transition: all 0.3s ease;
    }
    .stSidebar .stButton button:hover {
        background-color: #FFFFFF;
        border: 2px solid #CE93D8;
        color: #000;
        transform: scale(1.02);
    }
    .stSidebar .stButton button:focus {
        background-color: #FFFFFF;
        border: 2px solid #CE93D8;
        box-shadow: 0 0 10px #CE93D8;
    }
    
    /* Contenedor de la tarjeta (hecho con st.container(border=True)) */
    [data-testid="stVerticalBlockBorderWrapper"] {
        background-color: #FFFFFFCC;
        border-radius: 12px;
        padding: 10px !important;
    }

    </style>
    """, unsafe_allow_html=True)


# ========== APLICACIÓN PRINCIPAL (PORTAL DE CLIENTE) ==========

def main_portal():
    
    st.set_page_config(
        page_title="Mapa de Talento",
        page_icon="🗺️",
        layout="wide"
    )
    
    # Aplicar el mismo diseño "fresco"
    setup_portal_design()
    
    # --- 1. Inicializar Estado ---
    if 'selected_phase' not in st.session_state:
        st.session_state.selected_phase = PIPELINE_STAGES[0] # Empezar en "Nuevo"
    
    # --- 2. Conectar a la Base de Datos (Google Sheets) ---
    creds = get_google_creds(Config.GDRIVE_TOKEN_FILE)
    
    # (Manejo de autenticación para Streamlit Cloud)
    if "google_creds_valid" not in st.session_state:
        st.session_state.google_creds_valid = False
    if creds is not None and not st.session_state.google_creds_valid:
        st.session_state.google_creds_valid = True
        st.rerun()
    if not st.session_state.google_creds_valid:
         st.info("Por favor, completa la autenticación de Google en la terminal (consola) para continuar.")
         return
         
    worksheet = connect_to_gsheet(creds)
    
    if worksheet is None:
        st.error("Error fatal: No se pudo conectar a la base de datos de talento.")
        return
        
    df_completo = load_data_from_gsheet(worksheet)
    
    if df_completo.empty:
        st.info("Aún no se han clasificado candidatos.")
        return
        
    # --- 3. Sidebar (Filtros y Navegación) ---
    
    st.sidebar.markdown('<h1 style="text-align: left; font-size: 2.5rem; margin-bottom: 0; color: #81D4FA;">🗺️</h1>', unsafe_allow_html=True)
    st.sidebar.title("Mapa de Talento")

    # Filtro por Proceso
    lista_procesos = sorted(df_completo['Proceso'].unique())
    proceso_seleccionado = st.sidebar.selectbox("Selecciona un Proceso:", lista_procesos)
    
    st.sidebar.markdown("---")
    st.sidebar.subheader("Fases del Proceso")

    # Botones de navegación de Fases
    for stage in PIPELINE_STAGES:
        if st.sidebar.button(stage, use_container_width=True):
            st.session_state.selected_phase = stage
    
    # Botón de Rechazados al final
    st.sidebar.markdown("---")
    if st.sidebar.button(RECHAZADO_STAGE, use_container_width=True):
        st.session_state.selected_phase = RECHAZADO_STAGE

    # --- NUEVO: Información sobre entrevistas ---
    st.sidebar.markdown("---")
    st.sidebar.markdown("**💡 Funcionalidades nuevas:**")
    st.sidebar.markdown("• **Subir entrevistas** en fases: 🗓️ Agendar, 🎤 Entrevistado, ✅ Aceptado")
    st.sidebar.markdown("• **Ver todos los informes** en cada candidato")
    
    # --- 4. Aplicar Filtros (lógica principal) ---
    
    df_proceso = df_completo[df_completo['Proceso'] == proceso_seleccionado].copy()
    
    # Solo mostrar candidatos relevantes para el cliente
    df_cliente = df_proceso[
        (df_proceso['Clasificación'].isin(['🌟 Óptimo', '✅ Adecuado'])) |
        (df_proceso['Estado_Pipeline'].isin(PIPELINE_STAGES + [RECHAZADO_STAGE]))
    ]
    df_cliente = df_cliente[df_cliente['Estado_Pipeline'] != 'Descartado (Reclutador)']
    
    # Filtrar por la fase seleccionada en el sidebar
    df_fase_actual = df_cliente[df_cliente['Estado_Pipeline'] == st.session_state.selected_phase].copy()
    
    # --- 5. Mostrar la Página Principal (El "Tablero") ---
    
    st.header(f"{st.session_state.selected_phase}")
    st.caption(f"Proceso: {proceso_seleccionado} | {len(df_fase_actual)} candidato(s) en esta fase.")
    
    # Indicador visual para fases con entrevistas
    if st.session_state.selected_phase in FASES_ENTREVISTA:
        st.info("📋 **Modo entrevistas activado**: Puedes subir informes de entrevista en cada candidato")
    
    st.markdown("---")

    # Separar en dos columnas: Óptimos y Adecuados
    col_optimos, col_adecuados = st.columns(2)
    
    # Encontrar el índice del estado actual (para pasarlo a los botones)
    try:
        current_stage_index = PIPELINE_STAGES.index(st.session_state.selected_phase)
    except ValueError:
        current_stage_index = -1 # Es "Rechazado" o un estado no estándar
    
    # --- Columna de Óptimos ---
    with col_optimos:
        st.subheader(f"🌟 Óptimos ({len(df_fase_actual[df_fase_actual['Clasificación'] == '🌟 Óptimo'])})")
        
        df_optimos = df_fase_actual[df_fase_actual['Clasificación'] == '🌟 Óptimo'].copy()
        
        if df_optimos.empty:
            st.info("No hay candidatos óptimos en esta fase.")
        else:
            df_optimos['Fecha'] = pd.to_datetime(df_optimos['Fecha'], errors='coerce')
            df_optimos = df_optimos.sort_values(by="Fecha", ascending=False)
            
            for index, candidato_row in df_optimos.iterrows():
                mostrar_ficha_candidato(candidato_row, current_stage_index)

    # --- Columna de Adecuados ---
    with col_adecuados:
        st.subheader(f"✅ Adecuados ({len(df_fase_actual[df_fase_actual['Clasificación'] == '✅ Adecuado'])})")
        
        df_adecuados = df_fase_actual[df_fase_actual['Clasificación'] == '✅ Adecuado'].copy()
        
        if df_adecuados.empty:
            st.info("No hay candidatos adecuados en esta fase.")
        else:
            df_adecuados['Fecha'] = pd.to_datetime(df_adecuados['Fecha'], errors='coerce')
            df_adecuados = df_adecuados.sort_values(by="Fecha", ascending=False)
            
            for index, candidato_row in df_adecuados.iterrows():
                mostrar_ficha_candidato(candidato_row, current_stage_index)
    
    # --- Lógica especial para la vista de Rechazados ---
    if st.session_state.selected_phase == RECHAZADO_STAGE:
        col_optimos.empty() # Limpiar las columnas
        col_adecuados.empty() # Limpiar las columnas
        st.subheader("Candidatos Rechazados por el Cliente")
        
        if df_fase_actual.empty:
            st.info("No hay candidatos rechazados en este proceso.")
        else:
            for index, candidato_row in df_fase_actual.iterrows():
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.markdown(f"**{candidato_row['Archivo']}** ({candidato_row['Clasificación']})")
                with col2:
                    if st.button("Restaurar a 'Nuevo'", key=f"restore_{candidato_row['Archivo']}", type="secondary"):
                        mover_candidato(candidato_row['Archivo'], PIPELINE_STAGES[0])


if __name__ == "__main__":
    main_portal()
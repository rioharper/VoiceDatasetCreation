'''
A speech dataset generation tool.
This tool automagically generates and records sentences for use in speech datasets.
'''

import re
import sys
import math
import glob
import wave
import random
import pyaudio
from PySide2.QtCore import Qt, Slot, QTimer
from PySide2.QtGui import QPainter
from PySide2.QtWidgets import (QAction, QApplication, QHeaderView, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QPushButton, QTableWidget, QTableWidgetItem, QListWidget,
                               QListWidgetItem, QVBoxLayout, QWidget, QGroupBox, QSizePolicy, QFileDialog, QProgressDialog)
from pathlib import Path
from pydub import AudioSegment

def natural_keys(text):
    '''
    alist.sort(key=natural_keys) sorts in human order
    Source: https://stackoverflow.com/a/5967539/7614083
    '''
    return [(int(c) if c.isdigit() else c) for c in re.split(r'(\d+)', text)]

def detect_leading_silence(sound, silence_threshold=-40.0, chunk_size=10):
    trim_ms = 0
    assert chunk_size > 0
    while sound[trim_ms:trim_ms+chunk_size].dBFS < silence_threshold and trim_ms < len(sound):
        trim_ms += chunk_size

    return trim_ms

class Widget(QWidget):
    def __init__(self):
        QWidget.__init__(self)

        # Left panel
        self.remove_sentence = QPushButton('Remove Sentence')
        self.remove_sentence.setEnabled(False)
        self.remove_sentence.clicked.connect(self.on_remove_sentence)

        self.generate_sentence = QPushButton('Generate Sentence')
        self.generate_sentence.setEnabled(False)

        self.left_panel = QVBoxLayout()
        self.left_panel.setMargin(10)

        # Settings widgets
        self.dataset_name = QLineEdit()
        self.output_directory_label = QLineEdit()
        self.output_directory_label.setReadOnly(True)
        self.output_directory_picker = QPushButton('...')
        self.output_directory_picker.setMaximumWidth(30)
        self.full_dataset_root_path_label = QLabel()
        self.full_dataset_root_path_label.setStyleSheet('color: gray')

        # Settings groupbox
        self.update_settings = QPushButton('Update')
        self.update_settings.setEnabled(False)
        self.settings_groupbox = QGroupBox('Settings')
        settings_vbox = QVBoxLayout()
        settings_vbox.addWidget(QLabel('Dataset Name'))
        settings_vbox.addWidget(self.dataset_name)

        settings_vbox.addWidget(QLabel('Output Directory'))
        output_directory_hbox = QHBoxLayout()
        output_directory_hbox.addWidget(self.output_directory_label)
        output_directory_hbox.addWidget(self.output_directory_picker)
        settings_vbox.addLayout(output_directory_hbox)
        settings_vbox.addWidget(self.full_dataset_root_path_label)

        settings_vbox.addWidget(self.update_settings)
        self.settings_groupbox.setLayout(settings_vbox)
        self.settings_groupbox.setFixedHeight(180)
        self.left_panel.addWidget(self.settings_groupbox)

        # Settings slots and signals
        self.dataset_name.textChanged[str].connect(self.check_update_settings_disable)
        self.output_directory_label.textChanged[str].connect(self.check_update_settings_disable)
        self.update_settings.clicked.connect(self.on_update_settings)
        self.output_directory_picker.clicked.connect(self.on_open_output_directory)

        # Generator widgets
        self.generator_sources = QListWidget()
        self.add_generator_source = QPushButton('Add')
        self.remove_generator_source = QPushButton('Remove')
        self.remove_generator_source.setEnabled(False)

        self.generated_sentence_label = QLabel()
        sentence_font = self.generated_sentence_label.font()
        sentence_font.setPointSize(18)
        sentence_font.setBold(True)
        self.generated_sentence_label.setFont(sentence_font)
        self.generated_sentence_label.setAlignment(Qt.AlignCenter)
        self.generated_sentence_label.setWordWrap(True)

        # Generator groupbox
        self.generator_groupbox = QGroupBox('Generator')
        generator_vbox = QVBoxLayout()
        generator_vbox.addWidget(QLabel('Source Datasets'))
        generator_vbox.addWidget(self.generator_sources)

        generator_buttons_hbox = QHBoxLayout()
        generator_buttons_hbox.addWidget(self.add_generator_source)
        generator_buttons_hbox.addWidget(self.remove_generator_source)
        generator_vbox.addLayout(generator_buttons_hbox)

        self.generator_groupbox.setLayout(generator_vbox)
        self.generator_groupbox.setFixedHeight(150)
        self.left_panel.addWidget(self.generator_groupbox)

        self.generated_sentence_groupbox = QGroupBox()
        generate_vbox = QVBoxLayout()
        generate_vbox.setAlignment(Qt.AlignTop)
        generate_vbox.addWidget(self.generate_sentence)
        generate_vbox.addWidget(self.generated_sentence_label)
        self.generated_sentence_groupbox.setLayout(generate_vbox)
        self.left_panel.addWidget(self.generated_sentence_groupbox)

        self.record = QPushButton('Record Sentence')
        self.left_panel.addWidget(self.record)

        self.record.setEnabled(False)
        self.record.clicked.connect(self.on_record_clicked)

        # Generator slots and signals
        self.generator_sources.model().rowsInserted.connect(self.on_generator_source_data_changed)
        self.generator_sources.model().rowsRemoved.connect(self.on_generator_source_data_changed)
        self.generator_sources.selectionModel().selectionChanged.connect(self.on_generator_source_selection_changed)
        self.add_generator_source.clicked.connect(self.on_add_generator_source)
        self.remove_generator_source.clicked.connect(self.on_remove_generator_source)

        self.left_panel.addWidget(self.remove_sentence)


        # Table panel
        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(['Recording', 'Transcription'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.selectionModel().selectionChanged.connect(self.on_table_selection_changed)

        # QWidget Layout
        self.layout = QHBoxLayout()
        self.left_panel.setAlignment(Qt.AlignTop)
        self.layout.addLayout(self.left_panel, 2)
        self.layout.addWidget(self.table, 3)

        # Set the layout to the QWidget
        self.setLayout(self.layout)
        self.generate_sentence.clicked.connect(self.on_generate_sentence)

        self.audio_record_timer = QTimer()
        self.audio_record_timer.timeout.connect(self.on_audio_record_timer_tick)
        self.is_recording = False

        self.AUDIO_TIMER_INTERVAL_MS = 1
        self.audio_interface = pyaudio.PyAudio()
        self.audio_stream = None
        self.current_audio_recording_frames = None

    @Slot()
    def on_update_settings(self):
        self.update_settings.setEnabled(False)
        self.check_record_enable()

        # Create new dataset folder
        output_directory_path = Path(self.output_directory_label.text()) / self.dataset_name.text()
        output_directory_path.mkdir(parents=True, exist_ok=True)

        items_to_add = []
        # If there already is a 'wavs' folder, we should load in all wave files...
        waves_output_directory_path = output_directory_path / 'wavs'
        transcript_file_path = output_directory_path / 'metadata.csv'
        if waves_output_directory_path.exists() and transcript_file_path.exists() and transcript_file_path.is_file():
            transcription_map = {}
            with open(transcript_file_path, 'r') as transcript_file:
                for line in transcript_file.readlines():
                    recording_id, transcription = map(str.strip, line.split('|'))
                    transcription_map[str(Path(recording_id))] = transcription

            files = sorted(glob.glob(str(waves_output_directory_path / '*.wav')), key=natural_keys)
            for file in files:
                recording_id = str(Path(file).relative_to(output_directory_path))
                # print(recording_id in transcription_map , '*', recording_id, '*', transcription_map)
                if not recording_id in transcription_map: continue
                items_to_add.append((recording_id, transcription_map[recording_id]))

        # TODO: Move to new folder, if there is any.
        for i in range(self.table.rowCount()):
            print(self.table.rowAt(i))

        for item in items_to_add:
            self.add_transcription(*item, recreate_csv=False)

        self.create_transcript_csv()
        self.full_dataset_root_path_label.setText(str(output_directory_path))

    @Slot()
    def on_open_output_directory(self):
        path = str(QFileDialog.getExistingDirectory(self, 'Select Directory'))
        self.output_directory_label.setText(path)

    @Slot()
    def check_update_settings_disable(self, s):
        if not self.dataset_name.text() or not self.output_directory_label.text():
            self.update_settings.setEnabled(False)
        else:
            self.update_settings.setEnabled(True)

    @Slot()
    def on_generator_source_data_changed(self):
        self.generate_sentence.setEnabled(self.generator_sources.count() != 0)

    @Slot()
    def on_generator_source_selection_changed(self, selection):
        self.remove_generator_source.setEnabled(len(self.generator_sources.selectedItems()) != 0)

    @Slot()
    def on_add_generator_source(self):
        source_filepaths = QFileDialog.getOpenFileNames(self, 'Select Generator Sources')[0]
        if len(source_filepaths) == 0: return
        for filepath_str in source_filepaths:
            if len(self.generator_sources.findItems(filepath_str, Qt.MatchExactly)) > 0: continue

            filepath = Path(filepath_str)
            if not filepath.exists() or not filepath.is_file(): continue
            item = QListWidgetItem(filepath_str)
            with open(filepath, 'r', encoding='utf8', errors='ignore') as file:
                lines = file.readlines()

            if len(lines) == 0: continue

            item.setData(Qt.UserRole, lines)
            self.generator_sources.addItem(item)

    @Slot()
    def on_remove_generator_source(self):
        selected_items = self.generator_sources.selectedItems()
        if len(selected_items) == 0:
            # If the clicked command was called and somehow nothing is selected, something went wrong...
            # Therefore, disable the button so that it doesn't happen again!
            self.remove_generator_source.setEnabled(False)
            return

        if len(selected_items) == self.generator_sources.count():
            self.remove_generator_source.setEnabled(False)

        for item in selected_items:
            self.generator_sources.takeItem(self.generator_sources.row(item))

    @Slot()
    def on_table_selection_changed(self, selection):
        self.remove_sentence.setEnabled(len(self.table.selectedItems()) != 0)

    @Slot()
    def on_remove_sentence(self):
        selected_items = self.table.selectedItems()
        if len(selected_items) == 0:
            # If the clicked command was called and somehow nothing is selected, something went wrong...
            # Therefore, disable the button so that it doesn't happen again!
            self.remove_sentence.setEnabled(False)
            return

        if len(selected_items) == self.table.rowCount():
            self.remove_sentence.setEnabled(False)

        for item in selected_items:
            self.table.removeRow(self.table.indexFromItem(item).row())

        self.create_transcript_csv()

    @Slot()
    def on_generate_sentence(self):
        if self.generator_sources.count() == 0: return

        # Choose a random source
        index = random.randint(0, self.generator_sources.count() - 1)
        widget_item = self.generator_sources.item(index)
        line = random.choice(widget_item.data(Qt.UserRole)).strip()
        self.generated_sentence_label.setText(line)
        self.check_record_enable()
        

    @Slot()
    def check_record_enable(self):
        # Record button is enabled IFF we have a:
        #   - dataset name;
        #   - dataset output directory and VALID;
        #   - generated sentence.
        has_dataset_name = self.dataset_name.text()
        has_dataset_output_directory = self.output_directory_label.text()
        if has_dataset_output_directory:
            output_directory_path = Path(self.output_directory_label.text())
            has_dataset_output_directory = output_directory_path.exists() and not output_directory_path.is_file()

        has_generated_sentence = self.generated_sentence_label.text()
        self.record.setEnabled(all([has_dataset_name, has_dataset_output_directory, has_generated_sentence]))

    @Slot()
    def on_record_clicked(self):
        self.is_recording = not self.is_recording
        if self.is_recording:
            self.current_audio_recording_frames = []
            self.audio_stream = self.audio_interface.open(format=pyaudio.paInt16, channels=1, rate=22050, frames_per_buffer=1024, input=True)
            self.audio_record_timer.start(self.AUDIO_TIMER_INTERVAL_MS)
        else:
            # Save the last parts of the audio...
            self.on_audio_record_timer_tick(force=True)
            self.audio_stream.stop_stream()
            self.audio_stream.close()
            self.audio_record_timer.stop()

            output_directory_path = Path(self.output_directory_label.text()) / self.dataset_name.text()
            waves_output_directory_path = output_directory_path / 'wavs'
            if not waves_output_directory_path.exists():
                waves_output_directory_path.mkdir(parents=True, exist_ok=True)

            new_row_index = self.table.rowCount()
            destination_file_path = waves_output_directory_path / '{}{}.wav'.format(self.dataset_name.text(), new_row_index + 1)
            waveform = wave.open(str(destination_file_path), 'wb')
            waveform.setnchannels(self.audio_stream._channels)
            waveform.setsampwidth(self.audio_interface.get_sample_size(self.audio_stream._format))
            waveform.setframerate(self.audio_stream._rate)
            waveform.writeframes(b''.join(self.current_audio_recording_frames))
            waveform.close()
            
            
            wavspathout = str(destination_file_path.relative_to(output_directory_path)) #appends wavs/ and .wav to metadata.csv, used in some datasets
            ljspeechout = wavspathout.replace(".wav", "").replace("wavs/", "") #removes wavs/ and .wav from metadata.csv, used in ljspeech format
            self.add_transcription(str(ljspeechout), self.generated_sentence_label.text()) #remove ljspeechout with wavspathout to replace format

        self.record.setText('Record Sentence' if not self.is_recording else 'Stop Recording')

    def add_transcription(self, recording_id, transcription, recreate_csv=True):
        new_row_index = self.table.rowCount()

        self.table.insertRow(new_row_index)
        self.table.setItem(new_row_index, 0, QTableWidgetItem(recording_id))
        self.table.setItem(new_row_index, 1, QTableWidgetItem(transcription))

        if recreate_csv: self.create_transcript_csv()

    def create_transcript_csv(self):
        transcript_file_path = Path(self.output_directory_label.text()) / self.dataset_name.text() / 'metadata.csv'
        with open(transcript_file_path, 'w+') as file:
            for i in range(self.table.rowCount()):
                recording_id = self.table.item(i, 0).text()
                transcription = self.table.item(i, 1).text()
                file.write('{}|{}\n'.format(recording_id, transcription))

    def on_audio_record_timer_tick(self, force=False):
        if not force and not self.is_recording:
            self.audio_record_timer.stop()
            return

        data = self.audio_stream.read(self.audio_stream._frames_per_buffer)
        self.current_audio_recording_frames.append(data)

    @Slot()
    def quit_application(self):
        self.audio_interface.terminate()
        QApplication.quit()

class MainWindow(QMainWindow):
    def __init__(self, widget):
        QMainWindow.__init__(self)
        self.setWindowTitle('Speech Dataset Wizard')
        self.menu = self.menuBar()

        # File menu
        self.file_menu = self.menu.addMenu('File')

        new_action = QAction('New', self)
        new_action.setShortcut('Ctrl+N')
        new_action.triggered.connect(self.new_file)

        exit_action = QAction('Exit', self)
        exit_action.setShortcut('Ctrl+Q')
        exit_action.triggered.connect(self.exit_app)

        self.file_menu.addAction(new_action)
        self.file_menu.addSeparator()
        self.file_menu.addAction(exit_action)

        # Process menu
        self.process_menu = self.menu.addMenu('Process')
        self.process_menu.setEnabled(False)

        trim_silence_action = QAction('Trim Silence', self)
        trim_silence_action.triggered.connect(self.trim_silence)
        self.process_menu.addAction(trim_silence_action)

        widget.table.model().rowsInserted.connect(self.on_table_data_changed)
        widget.table.model().rowsRemoved.connect(self.on_table_data_changed)

        self.setCentralWidget(widget)

    @Slot()
    def on_table_data_changed(self):
        centralWidget = self.centralWidget()
        self.process_menu.setEnabled(centralWidget.table.rowCount() > 0)

    @Slot()
    def new_file(self, checked):
        pass

    @Slot()
    def exit_app(self, checked):
        QApplication.quit()

    @Slot()
    def trim_silence(self, checked):
        centralWidget = self.centralWidget()
        progress = QProgressDialog('Computing trimmed audio...', 'Cancel', 0, 2 * (centralWidget.table.rowCount() - 1), self)
        progress.setMinimumDuration(0)
        progress.setWindowTitle('Trimming silence...')
        progress.setWindowModality(Qt.WindowModal)

        trimmed_Y = []
        for i in range(centralWidget.table.rowCount()):
            progress.setValue(i)
            if progress.wasCanceled(): return

            recording_id = centralWidget.table.item(i, 0).text()
            filepath = Path(centralWidget.output_directory_label.text()) / centralWidget.dataset_name.text() / recording_id
            progress.setLabelText('Computing trimmed audio...{}'.format(recording_id))

            # Trim silence
            sound = AudioSegment.from_file(filepath, format=filepath.suffix.replace('.', ''))
            start_trim = detect_leading_silence(sound)
            end_trim = detect_leading_silence(sound.reverse())
            trimmed_sound = sound[start_trim:len(sound)-end_trim]
            trimmed_Y.append((filepath, trimmed_sound))

        # Output
        for i in range(len(trimmed_Y)):
            filepath, trimmed_sound = trimmed_Y[i]
            progress.setValue(len(trimmed_Y) - 1 + i)
            progress.setLabelText('Saving {}'.format(recording_id))
            trimmed_sound.export(filepath, format=filepath.suffix.replace('.', ''))

if __name__ == '__main__':
    # Qt Application
    app = QApplication(sys.argv)
    # QWidget
    widget = Widget()
    # QMainWindow using QWidget as central widget
    window = MainWindow(widget)
    window.resize(1080, 600)
    window.show()

    # Execute application
    sys.exit(app.exec_())

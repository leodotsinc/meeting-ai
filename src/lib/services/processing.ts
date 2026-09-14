import { prisma } from "@/lib/db/prisma";
import { ProcessingStatus } from "@prisma/client";
import { transcribeAudio, getSpeakerColor } from "./transcription";
import { analyzeMeeting } from "./gemini";
import { log } from "@/lib/logger";
import { withDeploymentLease } from "@/lib/server/deployment-lease";

export async function processMeeting(meetingId: string): Promise<void> {
  return withDeploymentLease(() => runMeetingProcessing(meetingId));
}

async function runMeetingProcessing(meetingId: string): Promise<void> {
  const startTime = Date.now();
  const meeting = await prisma.meeting.findUnique({
    where: { id: meetingId },
  });

  if (!meeting) {
    throw new Error(`Meeting not found: ${meetingId}`);
  }

  try {
    // Update status to transcribing
    await prisma.meeting.update({
      where: { id: meetingId },
      data: { status: ProcessingStatus.TRANSCRIBING, processingError: null },
    });

    log.processingStart(meetingId, meeting.title);
    log.transcriptionStart(meetingId);

    // Transcribe audio (AssemblyAI with automatic language detection)
    const transcriptionResult = await transcribeAudio(meeting.storagePath);

    // Update status to analyzing
    await prisma.meeting.update({
      where: { id: meetingId },
      data: { status: ProcessingStatus.ANALYZING },
    });

    log.transcriptionComplete(
      meetingId,
      transcriptionResult.utterances.length,
      transcriptionResult.speakers.length
    );
    log.analysisStart(meetingId);

    // Analyze with Gemini (using detected language and custom instructions if provided)
    const analysisResult = await analyzeMeeting(
      transcriptionResult.fullText,
      transcriptionResult.detectedLanguage,
      meeting.aiInstructions
    );

    log.analysisComplete(
      meetingId,
      analysisResult.topics.length,
      analysisResult.actionItems.length
    );
    log.debug("Saving results to database", { meetingId });

    // Save everything in a single transaction
    await prisma.$transaction(async (tx) => {
      // Clean up any existing data from previous attempts
      await tx.utterance.deleteMany({
        where: { transcript: { meetingId } },
      });
      await tx.transcript.deleteMany({ where: { meetingId } });
      await tx.speaker.deleteMany({ where: { meetingId } });
      await tx.analysis.deleteMany({ where: { meetingId } });

      // Create a map of speakerIndex to identified names from Gemini
      const speakerNameMap = new Map(
        analysisResult.speakerNames.map((s) => [s.speakerIndex, s.name])
      );

      // Create speakers with identified names
      const speakerRecords = await Promise.all(
        transcriptionResult.speakers.map((speakerIndex) =>
          tx.speaker.create({
            data: {
              meetingId,
              speakerIndex,
              label: speakerNameMap.get(speakerIndex) || null,
              color: getSpeakerColor(speakerIndex),
            },
          })
        )
      );

      // Create a map of speakerIndex to speaker record id
      const speakerMap = new Map(
        speakerRecords.map((s) => [s.speakerIndex, s.id])
      );

      // Create transcript with utterances
      await tx.transcript.create({
        data: {
          meetingId,
          fullText: transcriptionResult.fullText,
          rawResponse: transcriptionResult.rawResponse as object,
          utterances: {
            create: transcriptionResult.utterances.map((u, index) => ({
              speakerId: speakerMap.get(u.speaker)!,
              text: u.text,
              startTime: u.start,
              endTime: u.end,
              confidence: u.confidence,
              orderIndex: index,
            })),
          },
        },
      });

      // Create analysis record
      await tx.analysis.create({
        data: {
          meetingId,
          summary: analysisResult.summary,
          meetingDocument: analysisResult.meetingDocument || null,
          topics: JSON.parse(JSON.stringify(analysisResult.topics)),
          keyPoints: JSON.parse(JSON.stringify(analysisResult.keyPoints)),
          actionItems: JSON.parse(JSON.stringify(analysisResult.actionItems)),
          rawResponse: JSON.parse(JSON.stringify(analysisResult.rawResponse)),
        },
      });

      // Update meeting with final status
      await tx.meeting.update({
        where: { id: meetingId },
        data: {
          status: ProcessingStatus.COMPLETED,
          duration: Math.round(transcriptionResult.duration),
          language: transcriptionResult.detectedLanguage,
          processedAt: new Date(),
        },
      });
    });

    log.processingComplete(meetingId, meeting.title, Date.now() - startTime);
  } catch (error) {
    log.processingError(meetingId, error instanceof Error ? error : String(error));

    // Update status to failed
    await prisma.meeting.update({
      where: { id: meetingId },
      data: {
        status: ProcessingStatus.FAILED,
        processingError:
          error instanceof Error ? error.message : "Unknown error",
      },
    });

    throw error;
  }
}
